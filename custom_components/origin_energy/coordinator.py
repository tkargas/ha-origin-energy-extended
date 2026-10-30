"""DataUpdateCoordinator for Origin Energy.

Origin/Kraken hands us clean usage totals with real calendar dates
attached (not a live incrementing meter), so rather than faking a
continuously-increasing sensor, usage and cost are written directly into
Home Assistant's long-term statistics as an external source. That's the
mechanism the Energy dashboard expects for exactly this shape of data,
and is also what core's own `opower` integration (utility billing data)
does -- this coordinator follows the same pattern.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    UsageWindow,
    chunk_days,
    parse_api_datetime,
    pick_service,
)
from .auth import (
    OriginEnergyAuthError,
    OriginEnergySessionExpired,
    async_get_tokens,
    current_session_cookie,
    seed_session_cookie,
)
from .const import (
    CONF_AGREEMENT_ID,
    CONF_BACKFILL_COMPLETE,
    CONF_MANUAL_STATISTIC_ID,
    CONF_SESSION_COOKIE,
    CONF_TIME_UNIT,
    CONF_TIMEZONE,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_TIME_UNIT,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    FIRST_RUN_HISTORY_DAYS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_UPDATE_INTERVAL_HOURS,
    OVERLAP_DAYS,
)
from .manual_floor import (
    ManualFloor,
    configured_statistic_id,
    floor_from_last_statistics,
    later_start,
    not_before_floor,
)

_LOGGER = logging.getLogger(__name__)

type OriginEnergyConfigEntry = ConfigEntry["OriginEnergyCoordinator"]

# Origin declares the units it is answering in rather than fixing them, so
# convert from what the API actually says. Writing a wrong-magnitude value
# into long-term statistics is painful to undo, so an unrecognised unit is
# treated as a hard failure rather than guessed at.
ENERGY_TO_KWH = {"WATT_HOUR": 0.001, "KILOWATT_HOUR": 1.0, "MEGAWATT_HOUR": 1000.0}
COST_TO_DOLLARS = {"CENT": 0.01, "DOLLAR": 1.0}

# How far back to look for the statistic preceding a re-imported window.
# Comfortably longer than OVERLAP_DAYS so a daily poll always finds it.
BASELINE_LOOKBACK_DAYS = 30

# Mint a new access token once the current one is within this of expiring.
# Tokens last an hour and a long backfill can outlive one, which would
# otherwise fail every remaining window with a 401.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def configured_update_interval(config_entry: ConfigEntry) -> timedelta:
    """Poll interval from the entry's options, clamped to the sane range.

    Clamped rather than trusted: the value reaches here from a stored
    options dict, which survives downgrades and hand-edited
    .storage files, and a zero or negative interval would spin the
    coordinator against Origin's API as fast as the event loop allows.
    """
    hours = config_entry.options.get(
        CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS
    )
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = DEFAULT_UPDATE_INTERVAL_HOURS
    hours = min(max(hours, MIN_UPDATE_INTERVAL_HOURS), MAX_UPDATE_INTERVAL_HOURS)
    return timedelta(hours=hours)


def statistic_slug(value: str) -> str:
    """Reduce an identifier to the character set statistic IDs allow.

    Home Assistant validates external statistic IDs against
    ``[\\da-z_]+:[\\da-z_]+`` with no leading, trailing or doubled
    underscores, and rejects anything else outright. Origin's agreement IDs
    are plain digits on the accounts seen so far -- for which this is a
    no-op -- but nothing documents that, and a hyphen in someone else's ID
    would otherwise make every write raise.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown"


@dataclass
class OriginEnergyData:
    """Small marker object returned to any entities watching the coordinator."""

    last_updated: datetime


@dataclass
class _ImportCursor:
    """Running totals carried across query windows.

    Statistics store an absolute running sum per point, so an import has to
    walk forward in time accumulating totals. last_start also does the
    de-duplication: Origin's ranges are inclusive at both ends, so
    consecutive windows share a boundary point that must not be counted
    twice.
    """

    consumption_sum: float = 0.0
    cost_sum: float = 0.0
    last_start: datetime | None = None
    written: int = 0


class OriginEnergyCoordinator(DataUpdateCoordinator[OriginEnergyData]):
    """Fetch usage from Origin Energy and feed it into HA statistics."""

    config_entry: OriginEnergyConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: OriginEnergyConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=configured_update_interval(config_entry),
        )
        # No headers= here: Home Assistant replaces a session's default
        # headers with its own User-Agent right after construction, so
        # anything set here is silently dropped. The browser identity Origin's
        # WAF insists on is attached per request instead, in auth.py and api.py.
        self._session = async_create_clientsession(hass)
        seed_session_cookie(self._session, config_entry.data[CONF_SESSION_COOKIE])
        self.agreement_id: str = config_entry.data[CONF_AGREEMENT_ID]
        self.timezone = ZoneInfo(config_entry.data[CONF_TIMEZONE])
        self.time_unit: str = config_entry.data.get(CONF_TIME_UNIT, DEFAULT_TIME_UNIT)

        id_prefix = statistic_slug(self.agreement_id)
        self.consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
        self.cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_cost"

        self._manual_statistic_id = configured_statistic_id(
            config_entry.options, CONF_MANUAL_STATISTIC_ID
        )
        self._backfill_task: asyncio.Task[None] | None = None
        # Guards the statistics import so a background backfill and a
        # scheduled update can't interleave and corrupt the running sums.
        self._import_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    def async_adopt_update_interval(self) -> bool:
        """Take up a newly-configured poll interval. True if it changed.

        The caller needs the return value because this fires for *any*
        config entry write, and most of them are ours rather than the
        user's -- persisting a rolled session cookie, setting the
        backfill-complete flag. Reacting to those would re-anchor the
        poll schedule every time the cookie rotated.
        """
        interval = configured_update_interval(self.config_entry)
        if interval == self.update_interval:
            return False
        _LOGGER.debug(
            "Origin Energy poll interval changed from %s to %s",
            self.update_interval,
            interval,
        )
        self.update_interval = interval
        return True

    def async_adopt_manual_statistic(self) -> bool:
        """Take up a newly-configured manual statistic. True if it changed.

        Nothing is cached beyond the id itself: the statistic's last row is
        read on every update, so a changed id only needs a refresh to apply.
        """
        statistic_id = configured_statistic_id(
            self.config_entry.options, CONF_MANUAL_STATISTIC_ID
        )
        if statistic_id == self._manual_statistic_id:
            return False
        _LOGGER.debug(
            "Origin Energy manual statistic changed from %s to %s",
            self._manual_statistic_id,
            statistic_id,
        )
        self._manual_statistic_id = statistic_id
        return True

    async def async_close(self) -> None:
        """Stop the backfill before the entry finishes unloading.

        The session is deliberately left alone. Home Assistant registers an
        unload callback for any session created during config entry setup
        and detaches it itself; closing it here would be closing a session
        we don't own, which it warns about.
        """
        if self._backfill_task is not None:
            self._backfill_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._backfill_task

    def _persist_rolled_cookie(self) -> None:
        """Save the session cookie back if Auth0 rolled its expiry forward.

        Auth0 re-issues the SSO cookie on each successful silent
        authorization, pushing its ~100-day expiry out again. Writing the new
        value back to the config entry is what lets a once-imported session
        renew itself indefinitely instead of hard-expiring.
        """
        cookie = current_session_cookie(self._session)
        if not cookie or cookie == self.config_entry.data[CONF_SESSION_COOKIE]:
            return
        _LOGGER.debug("Origin Energy session cookie refreshed, persisting")
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_SESSION_COOKIE: cookie},
        )

    async def _async_api(self) -> OriginEnergyApiClient:
        """Return a client holding a currently-valid access token.

        Tokens last an hour, which a full history backfill can outlive, so
        the token is cached and re-minted as it nears expiry rather than
        being fetched once per update. The imported session cookie in the
        jar is what makes the silent exchange work.
        """
        now = dt_util.utcnow()
        if (
            self._access_token is not None
            and self._token_expires_at is not None
            and self._token_expires_at - now > TOKEN_REFRESH_MARGIN
        ):
            return OriginEnergyApiClient(self._session, self._access_token)

        try:
            tokens = await async_get_tokens(self._session)
        except OriginEnergySessionExpired as err:
            # Only a browser sign-in can fix this -- prompt the user to
            # re-import rather than retrying on a schedule forever.
            raise ConfigEntryAuthFailed(str(err)) from err
        except OriginEnergyAuthError as err:
            raise UpdateFailed(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Error connecting to Origin Energy: {err}") from err

        self._access_token = tokens.access_token
        self._token_expires_at = now + timedelta(seconds=tokens.expires_in)
        self._persist_rolled_cookie()
        return OriginEnergyApiClient(self._session, self._access_token)

    async def _async_update_data(self) -> OriginEnergyData:
        # Resolve auth up front so an expired session raises before any of
        # the import work, and surfaces as a re-auth prompt.
        await self._async_api()

        if self.config_entry.data.get(CONF_BACKFILL_COMPLETE):
            try:
                async with self._import_lock:
                    await self._async_import_recent()
            except OriginEnergyApiError as err:
                raise UpdateFailed(str(err)) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise UpdateFailed(f"Error connecting to Origin Energy: {err}") from err
        else:
            # A first backfill is one query per week of history; running it
            # inline would block Home Assistant's startup for minutes on a
            # long-standing account.
            self._start_backfill()

        return OriginEnergyData(last_updated=dt_util.utcnow())

    # ------------------------------------------------------------------
    # History backfill
    # ------------------------------------------------------------------

    def _start_backfill(self) -> None:
        """Kick off the history import without holding up setup."""
        if self._backfill_task is not None and not self._backfill_task.done():
            return
        _LOGGER.debug("Starting Origin Energy history backfill in the background")
        self._backfill_task = self.config_entry.async_create_background_task(
            self.hass, self._async_backfill(), name=f"{DOMAIN} history backfill"
        )

    async def _async_backfill(self) -> None:
        """Import all available history, oldest first, then mark it done.

        Only on success is CONF_BACKFILL_COMPLETE set, so a run interrupted
        by a restart simply starts over next time rather than leaving a
        permanent hole. Re-importing is idempotent: statistics are keyed by
        start time and the sums are re-derived from the same origin.
        """
        try:
            async with self._import_lock:
                start = await self._async_history_start()
                cursor = _ImportCursor()
                floor = await self._async_manual_floor()
                if floor is not None:
                    # Manual wins: nothing at or before its last hour. If this
                    # series already has rows (an interrupted earlier run) the
                    # sums must carry on from them, not restart at zero.
                    start = not_before_floor(start, floor)
                    cursor = await self._async_baseline_cursor(start) or cursor
                    cursor.last_start = later_start(cursor.last_start, floor)
                _LOGGER.debug("Backfilling Origin Energy usage from %s", start)
                cursor = await self._async_import(
                    start, dt_util.now(self.timezone), cursor
                )
        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed as err:
            # Nothing here can raise this into the coordinator's own
            # machinery, so the re-auth flow has to be started by hand.
            _LOGGER.warning("Origin Energy backfill needs re-authentication: %s", err)
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as err:  # noqa: BLE001 - retried on the next update
            _LOGGER.warning(
                "Origin Energy history backfill failed, will retry: %s", err
            )
            return

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_BACKFILL_COMPLETE: True},
        )
        _LOGGER.info(
            "Origin Energy history backfill complete, %d points imported",
            cursor.written,
        )

    async def _async_history_start(self) -> datetime:
        """Earliest instant worth asking for.

        Usage data begins when the agreement does, and earlier windows come
        back as buckets of MISSING points rather than as an error, so
        validFrom is the real floor. FIRST_RUN_HISTORY_DAYS only caps
        accounts that have been running longer than we care to import.
        """
        cap = dt_util.now(self.timezone) - timedelta(days=FIRST_RUN_HISTORY_DAYS)
        try:
            api = await self._async_api()
            services = await api.async_get_services()
        except OriginEnergyApiError as err:
            _LOGGER.debug("Could not read service validity (%s), using cap", err)
            return cap

        service = pick_service(services, self.agreement_id)
        valid_from = parse_api_datetime((service or {}).get("validFrom"))
        if valid_from is None:
            return cap
        return max(valid_from, cap)

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    async def _async_import_recent(self) -> None:
        """Extend the statistics with whatever has been published since."""
        floor = await self._async_manual_floor()
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, self.consumption_statistic_id, True, set()
        )
        if not last_stat:
            # The backfill was marked complete but produced nothing (a brand
            # new agreement with no readings yet). Try again from scratch.
            _LOGGER.debug("No Origin Energy statistics yet, re-running backfill")
            self._start_backfill()
            return

        last_point = dt_util.utc_from_timestamp(
            last_stat[self.consumption_statistic_id][0]["start"]
        )

        # Ask the API where real data currently ends. Anchoring the overlap
        # to that watermark rather than to wall-clock now means late-arriving
        # data still gets covered.
        now = dt_util.now(self.timezone)
        api = await self._async_api()
        probe = await api.async_get_usage(
            self.agreement_id, now - timedelta(days=2), now, self.time_unit
        )
        last_reading = probe.last_reading_date
        if last_reading is not None:
            _LOGGER.debug(
                "Origin Energy data available to %s, imported to %s",
                last_reading,
                last_point,
            )

        anchor = min(last_point, last_reading) if last_reading else last_point
        # The revision overlap must not reach back into the manual hours:
        # those hours belong to the manual statistic, so they are not ours
        # to (re)write.
        start = not_before_floor(anchor - timedelta(days=OVERLAP_DAYS), floor)
        end = max(last_reading or now, last_point) + timedelta(minutes=1)
        if floor is not None and start >= end:
            _LOGGER.debug(
                "Origin Energy data (to %s) has not passed the manual statistic "
                "(to %s) yet",
                last_reading,
                floor.last_start,
            )
            return

        cursor = await self._async_baseline_cursor(start)
        if cursor is None:
            # Couldn't establish what the sums were before the overlap
            # window, so re-deriving them would corrupt the series. Fall
            # back to appending only what is genuinely new.
            _LOGGER.debug("No baseline before %s, appending new points only", start)
            consumption_sum, cost_sum = await self._async_sums_at(last_point)
            cursor = _ImportCursor(consumption_sum, cost_sum, last_point)
            start = last_point

        # Skips every hour the manual statistic covers (a no-op without one).
        cursor.last_start = later_start(cursor.last_start, floor)
        cursor = await self._async_import(start, end, cursor)
        if not cursor.written:
            _LOGGER.debug("No new Origin Energy usage data since last update")

    async def _async_manual_floor(self) -> ManualFloor | None:
        """Last hour covered by the user's manual statistic, if one is set.

        Raises rather than returning None when the statistic is configured
        but holds nothing: carrying on would write Origin's data over the
        very hours the manual readings were meant to own, and a mistyped id
        is far more likely than an intentionally empty statistic.
        """
        statistic_id = self._manual_statistic_id
        if statistic_id is None:
            return None
        rows = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        floor = floor_from_last_statistics(rows, statistic_id)
        if floor is None:
            raise UpdateFailed(
                f"The manual statistic '{statistic_id}' has no data. Check the "
                "id in the Origin Energy options (Developer tools > Statistics "
                "lists the valid ids), or clear the option."
            )
        return floor

    async def _async_baseline_cursor(self, start: datetime) -> _ImportCursor | None:
        """Sums as of the last statistic strictly before `start`.

        Re-importing a window means recomputing every sum from there
        forward, which has to continue from whatever preceded it. Returns
        None when no preceding statistic can be found, since assuming zero
        would silently reset the series.
        """
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(days=BASELINE_LOOKBACK_DAYS),
            start,
            {self.consumption_statistic_id, self.cost_statistic_id},
            "hour",
            None,
            {"sum"},
        )
        consumption_rows = rows.get(self.consumption_statistic_id) or []
        if not consumption_rows:
            return None

        cost_rows = rows.get(self.cost_statistic_id) or []
        return _ImportCursor(
            consumption_sum=float(consumption_rows[-1]["sum"]),
            cost_sum=float(cost_rows[-1]["sum"]) if cost_rows else 0.0,
            last_start=None,
        )

    async def _async_sums_at(self, instant: datetime) -> tuple[float, float]:
        """Running sums at a known-present statistic."""
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            instant,
            instant + timedelta(seconds=1),
            {self.consumption_statistic_id, self.cost_statistic_id},
            "hour",
            None,
            {"sum"},
        )

        def _sum(statistic_id: str) -> float:
            entries = rows.get(statistic_id) or []
            return float(entries[0]["sum"]) if entries else 0.0

        return _sum(self.consumption_statistic_id), _sum(self.cost_statistic_id)

    # ------------------------------------------------------------------
    # Shared import machinery
    # ------------------------------------------------------------------

    def _statistic_metadata(self) -> tuple[StatisticMetaData, StatisticMetaData]:
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy consumption",
            source=DOMAIN,
            statistic_id=self.consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Origin Energy cost",
            source=DOMAIN,
            statistic_id=self.cost_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )
        return consumption_metadata, cost_metadata

    async def _async_import(
        self,
        start: datetime,
        end: datetime,
        cursor: _ImportCursor,
    ) -> _ImportCursor:
        """Fetch `start`..`end` in windows, writing statistics as they land.

        Origin caps a single query at a few days for sub-daily granularity,
        so long ranges are split. Writing each window as it arrives keeps
        memory flat over a multi-year backfill and lets the Energy dashboard
        fill in progressively.
        """
        consumption_metadata, cost_metadata = self._statistic_metadata()
        window = timedelta(days=chunk_days(self.time_unit))
        window_start = start

        while window_start < end:
            window_end = min(window_start + window, end)
            # Re-resolved per window: a long backfill outlives one token.
            api = await self._async_api()
            result = await api.async_get_usage(
                self.agreement_id, window_start, window_end, self.time_unit
            )

            consumption_stats, cost_stats = self._build_statistics(result, cursor)
            if consumption_stats:
                async_add_external_statistics(
                    self.hass, consumption_metadata, consumption_stats
                )
                async_add_external_statistics(self.hass, cost_metadata, cost_stats)
                cursor.written += len(consumption_stats)

            window_start = window_end

        return cursor

    def _build_statistics(
        self, result: UsageWindow, cursor: _ImportCursor
    ) -> tuple[list[StatisticData], list[StatisticData]]:
        """Turn one window's data points into statistics rows.

        Advances the cursor's running sums, which is why points must arrive
        in chronological order.
        """
        energy_scale = _scale(ENERGY_TO_KWH, result.consumption_unit, "WATT_HOUR", "energy")
        cost_scale = _scale(COST_TO_DOLLARS, result.cost_unit, "CENT", "cost")

        consumption_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []

        usable: list[tuple[datetime, dict]] = []
        for point in result.points:
            if point.get("totalConsumedEnergy") is None:
                # A MISSING bucket: either past lastReadingDate or a real
                # gap in the meter data. Both are padded out as full
                # buckets of nulls rather than being omitted.
                continue

            # Use Origin's own startDate as-is: HOURLY points arrive snapped
            # to the clock hour with the correct local offset (including the
            # half-hour offsets used by SA/NT), which is exactly the
            # alignment HA's statistics table wants.
            start = dt_util.parse_datetime(point["startDate"])
            if start is None:
                continue
            usable.append((start, point))

        # The gateway is asked for START_DATE_ASC and honours it, but the
        # running sums below are only correct in chronological order, and
        # ISO strings stop sorting chronologically at the end of daylight
        # saving: 02:00+11:00 and the 02:00+10:00 an hour later compare the
        # wrong way round as text. Sorting the parsed instants costs
        # nothing and removes the dependency entirely.
        usable.sort(key=lambda item: item[0])

        for start, point in usable:
            # Skips both the boundary point shared with the previous window
            # and anything already imported.
            if cursor.last_start is not None and start <= cursor.last_start:
                continue

            consumption_kwh = point["totalConsumedEnergy"] * energy_scale
            cost_dollars = (point.get("totalCost") or 0) * cost_scale

            cursor.consumption_sum += consumption_kwh
            cursor.cost_sum += cost_dollars
            cursor.last_start = start

            consumption_stats.append(
                StatisticData(
                    start=start, state=consumption_kwh, sum=cursor.consumption_sum
                )
            )
            cost_stats.append(
                StatisticData(start=start, state=cost_dollars, sum=cursor.cost_sum)
            )

        return consumption_stats, cost_stats


def _scale(table: dict[str, float], unit: str | None, default: str, label: str) -> float:
    """Look up a unit conversion, refusing to guess at an unknown one."""
    resolved = unit or default
    if resolved not in table:
        raise UpdateFailed(f"Unexpected {label} unit from Origin Energy: {unit}")
    return table[resolved]
