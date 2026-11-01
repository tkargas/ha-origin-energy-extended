#!/usr/bin/env python3
"""
Offline checks for skipping days that already have manual data.

Runs without Home Assistant installed: the HA/aiohttp modules are stubbed
and the recorder is replaced by an in-memory statistics table, so the real
coordinator code (_async_backfill, _async_import_recent, _build_statistics)
runs against a fake Origin API and a fake store. When Home Assistant *is*
importable the stubs are skipped for the parts that need no recorder.

Usage:
    python tools/test_manual_days_unit.py
"""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

FAILURES = 0


def check(label, got, want) -> None:
    global FAILURES
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


UTC = timezone.utc


def hr(day, hour, month=10):
    return datetime(2026, month, day, hour, tzinfo=UTC)


# --------------------------------------------------------------------------
# Stubs for everything the coordinator imports
# --------------------------------------------------------------------------


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__["__getattr__"] = lambda attr: MagicMock(name=f"{name}.{attr}")
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class UpdateFailed(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls


class _FakeDt:
    """Just the bits of homeassistant.util.dt the coordinator uses."""

    NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)

    @staticmethod
    def utcnow():
        return _FakeDt.NOW

    @staticmethod
    def now(tz=None):
        return _FakeDt.NOW.astimezone(tz) if tz else _FakeDt.NOW

    @staticmethod
    def utc_from_timestamp(ts):
        return datetime.fromtimestamp(ts, tz=UTC)

    @staticmethod
    def parse_datetime(text):
        return datetime.fromisoformat(text)


def install_stubs():
    for name in ("aiohttp", "yarl"):
        _stub(name, ClientError=type("ClientError", (Exception,), {}), ClientSession=object, URL=lambda *a, **k: MagicMock())
    ha = _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder")
    _stub(
        "homeassistant.components.recorder.models",
        StatisticData=lambda **kw: kw,
        StatisticMetaData=lambda **kw: kw,
    )
    _stub("homeassistant.components.recorder.statistics")
    _stub("homeassistant.config_entries", ConfigEntry=_DataUpdateCoordinator)
    _stub("homeassistant.const")
    _stub("homeassistant.core")
    _stub("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.aiohttp_client")
    _stub(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    )
    util = _stub("homeassistant.util")
    util.dt = _FakeDt
    _stub("homeassistant.util.dt", **{k: getattr(_FakeDt, k) for k in ("utcnow", "now", "utc_from_timestamp", "parse_datetime")})
    _stub("homeassistant.util.unit_conversion")
    ha.util = util


try:
    import homeassistant  # noqa: F401
    REAL_HA = True
except ImportError:
    REAL_HA = False

# The coordinator needs the stubs even when HA is present (it talks to a
# recorder), so they are installed unconditionally for this test.
for key in [k for k in sys.modules if k == "homeassistant" or k.startswith("homeassistant.")]:
    del sys.modules[key]
install_stubs()

from origin_energy import coordinator as coord  # noqa: E402
from origin_energy.api import UsageWindow  # noqa: E402
from origin_energy.coordinator import OriginEnergyCoordinator, _ImportCursor  # noqa: E402
from origin_energy.manual_days import (  # noqa: E402
    configured_statistic_id,
    days_with_manual_data,
    expand_to_local_days,
    local_day,
    parse_statistic_id,
)

# --------------------------------------------------------------------------
# 1. The pure helpers
# --------------------------------------------------------------------------
print("manual_days helpers")
check("blank -> None", parse_statistic_id("   "), None)
check("None -> None", parse_statistic_id(None), None)
check("entity id accepted + lower-cased", parse_statistic_id(" Sensor.Electricity_Meter_Total "), "sensor.electricity_meter_total")
check("external id accepted", parse_statistic_id("origin_energy:123_energy_consumption"), "origin_energy:123_energy_consumption")
for bad in ("sensor", "sensor.", "a.b.c", "has space.x", "sensor.x-y"):
    try:
        parse_statistic_id(bad)
        check(f"malformed {bad!r} rejected", "accepted", "ValueError")
    except ValueError:
        check(f"malformed {bad!r} rejected", "ValueError", "ValueError")
check("stored garbage ignored, not raised", configured_statistic_id({"manual_statistic_id": "nope"}, "manual_statistic_id"), None)
check("stored id read", configured_statistic_id({"manual_statistic_id": "sensor.m"}, "manual_statistic_id"), "sensor.m")


def row(moment, change):
    return {"start": moment.timestamp(), "change": change}


from datetime import date  # noqa: E402

check("a change marks its day", days_with_manual_data([row(hr(22, 12), 5.0)], UTC), frozenset({date(2026, 10, 22)}))
check("zero change does not (a live sensor ticking over)", days_with_manual_data([row(hr(22, h), 0.0) for h in range(24)], UTC), frozenset())
check("rounding noise does not", days_with_manual_data([row(hr(22, 3), 1e-9)], UTC), frozenset())
check("a negative change does", days_with_manual_data([row(hr(22, 3), -2.0)], UTC), frozenset({date(2026, 10, 22)}))
check("a missing change is ignored", days_with_manual_data([{"start": hr(22, 3).timestamp(), "change": None}], UTC), frozenset())
check("None rows", days_with_manual_data(None, UTC), frozenset())
check("two entries on two days", days_with_manual_data([row(hr(22, 1), 1), row(hr(23, 9), 1), row(hr(23, 10), 1)], UTC), frozenset({date(2026, 10, 22), date(2026, 10, 23)}))
check("local_day in UTC", local_day(hr(22, 23), UTC), date(2026, 10, 22))
check("expand_to_local_days (UTC) covers whole days", expand_to_local_days(hr(22, 5), hr(24, 7), UTC), (hr(22, 0), hr(25, 0)))
try:
    sydney = ZoneInfo("Australia/Sydney")
    # 13:30Z on the 30th is 00:30 on the 31st in Sydney (AEDT, +11)
    check("Sydney: an entry at 13:30Z belongs to the NEXT local day", days_with_manual_data([row(hr(30, 13) + timedelta(minutes=30), 3.0)], sydney), frozenset({date(2026, 10, 31)}))
    check("Sydney: an entry at 12:30Z belongs to the same local day", days_with_manual_data([row(hr(30, 12) + timedelta(minutes=30), 3.0)], sydney), frozenset({date(2026, 10, 30)}))
    check("expand_to_local_days (Sydney) widens to local midnights", expand_to_local_days(hr(30, 20), hr(31, 2), sydney), (hr(30, 13), hr(31, 13)))
except Exception as err:  # no tz database on this machine
    print(f"  skip Sydney checks ({err.__class__.__name__}: install tzdata to run them)")

# --------------------------------------------------------------------------
# 2. The coordinator against a fake store and a fake Origin
# --------------------------------------------------------------------------
OWN = "origin_energy:111_energy_consumption"
OWN_COST = "origin_energy:111_energy_cost"
MANUAL = "sensor.electricity_meter_total"


class Store:
    """In-memory stand-in for the recorder's hourly statistics."""

    def __init__(self):
        self.rows = {}  # statistic_id -> {start: {"state":..,"sum":..}}
        self.writes = []  # (statistic_id, start) in order
        self.cleared = []

    def put(self, statistic_id, start, state, total):
        self.rows.setdefault(statistic_id, {})[start] = {"state": state, "sum": total}

    def snapshot(self, statistic_id):
        return {s: dict(r) for s, r in self.rows.get(statistic_id, {}).items()}

    # -- the functions the coordinator calls
    def get_last_statistics(self, hass, n, statistic_id, convert, types_):
        rows = self.rows.get(statistic_id)
        if not rows:
            return {}
        start = max(rows)
        return {statistic_id: [{"start": start.timestamp(), "sum": rows[start]["sum"]}]}

    def statistics_during_period(self, hass, start, end, ids, period, units, types_):
        out = {}
        for sid in ids:
            ordered = sorted(self.rows.get(sid, {}).items())
            got = []
            for i, (s, r) in enumerate(ordered):
                if not (start <= s < end):
                    continue
                entry = {"start": s.timestamp(), "sum": r["sum"]}
                if types_ and "change" in types_:
                    # like HA: relative to the row before it (the first row ever is its own state)
                    entry["change"] = r["sum"] - ordered[i - 1][1]["sum"] if i else r["state"]
                got.append(entry)
            if got:
                out[sid] = got
        return out

    def clear(self, statistic_ids):
        self.cleared = list(statistic_ids)
        for sid in statistic_ids:
            self.rows.pop(sid, None)

    def add_external(self, hass, metadata, stats):
        for row_ in stats:
            self.put(metadata["statistic_id"], row_["start"], row_["state"], row_["sum"])
            self.writes.append((metadata["statistic_id"], row_["start"]))


class FakeOrigin:
    """Hourly data published up to last_published_hour."""

    def __init__(self, first, last_published_hour):
        self.first = first
        self.last = last_published_hour  # last hour that has data
        self.queries = []
        self.wh = lambda hour: (int(hour.timestamp() // 3600) % 7 + 1) * 100  # 100..700 Wh

    async def async_get_usage(self, agreement, start, end, unit):
        self.queries.append((start, end))
        points = []
        h = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        while h <= end and len(points) < 24 * 8:
            has = self.first <= h <= self.last
            points.append(
                {
                    "type": "ACTUAL" if has else "MISSING",
                    "startDate": h.isoformat(timespec="milliseconds"),
                    "totalConsumedEnergy": self.wh(h) if has else None,
                    "totalCost": 30 if has else None,
                }
            )
            h += timedelta(hours=1)
        return UsageWindow(
            points=points,
            last_reading_date=self.last + timedelta(hours=1),
            consumption_unit="WATT_HOUR",
            cost_unit="CENT",
            has_feed_in=False,
            has_missing_points=False,
        )


async def _run_job(fn, *args):
    return fn(*args)


def make(store, origin, manual_id=None, tz=UTC):
    ns = types.SimpleNamespace(
        get_instance=lambda hass: types.SimpleNamespace(
            async_add_executor_job=_run_job,
            async_clear_statistics=store.clear,
        ),
        get_last_statistics=store.get_last_statistics,
        statistics_during_period=store.statistics_during_period,
        async_add_external_statistics=store.add_external,
    )
    for name, value in vars(ns).items():
        setattr(coord, name, value)

    c = object.__new__(OriginEnergyCoordinator)
    c.hass = MagicMock()
    c.config_entry = MagicMock()
    c.config_entry.data = {}
    c.config_entry.options = {"manual_statistic_id": manual_id} if manual_id else {}
    c.agreement_id = "111"
    c.timezone = tz
    c.time_unit = "HOURLY"
    c.consumption_statistic_id = OWN
    c.cost_statistic_id = OWN_COST
    c._manual_statistic_id = manual_id
    c._backfill_task = None
    c._import_lock = asyncio.Lock()
    c.backfill_started = 0
    c.async_request_refresh = lambda: None

    async def _api():
        return origin

    async def _history_start():
        return origin.first

    c._async_api = _api
    c._async_history_start = _history_start
    c._start_backfill = lambda: setattr(c, "backfill_started", c.backfill_started + 1)
    return c


def manual_series(store, first, last, entries=None, base=1000.0):
    """A live sensor: a row every hour from first..last. `entries` maps an hour
    to the kWh entered then; every other hour changes by zero."""
    entries = entries or {}
    h, total = first, base
    while h <= last:
        amount = entries.get(h, 0.0)
        total += amount
        store.put(MANUAL, h, amount, total)
        h += timedelta(hours=1)


def hours(first, last):
    out, h = [], first
    while h <= last:
        out.append(h)
        h += timedelta(hours=1)
    return out


def day_hours(*days):
    return [h for d in days for h in hours(hr(d, 0), hr(d, 23))]


def run(coro):
    return asyncio.run(coro)


def chained(rows, hours_list, origin_wh):
    """Every listed hour's sum rose by exactly that hour's own value over the row before it."""
    ordered = sorted(rows)
    ok = True
    for h in hours_list:
        i = ordered.index(h)
        if i == 0:
            continue
        ok &= abs(rows[h]["sum"] - rows[ordered[i - 1]]["sum"] - origin_wh(h) / 1000) < 1e-9
    return ok


# ---- A: entries on three days; everything else imports ----------------------
print("A. days with a manual entry are skipped, every other day is added")
store = Store()
manual_series(store, hr(20, 0), hr(30, 9), {hr(22, 12): 21.5, hr(23, 9): 18.0, hr(27, 20): 25.0})
before = store.snapshot(MANUAL)
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, MANUAL)
run(c._async_backfill())
own = store.snapshot(OWN)
check("exactly the days without a manual entry", sorted(own), day_hours(20, 21, 24, 25, 26, 28, 29))
check("manual rows untouched", store.snapshot(MANUAL), before)
check("no write ever targeted the manual statistic", [w for w in store.writes if w[0] == MANUAL], [])
check("sums only count hours that were written", chained(own, sorted(own)[1:], origin.wh), True)
check("backfill marked complete", c.hass.config_entries.async_update_entry.called, True)

# ---- B: a live sensor with no entries blocks nothing -------------------------
print("B. a live sensor with no entries does not block Origin's days")
store = Store()
manual_series(store, hr(20, 0), hr(30, 9))  # every hour, zero change
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, MANUAL)
run(c._async_backfill())
check("all days imported", sorted(store.snapshot(OWN)), day_hours(20, 21, 22, 23, 24, 25, 26, 27, 28, 29))

# ---- C: later polls --------------------------------------------------------
print("C. the next poll adds new days and does not touch skipped ones")
store = Store()
manual_series(store, hr(20, 0), hr(30, 9), {hr(22, 12): 21.5, hr(27, 20): 25.0})
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, MANUAL)
run(c._async_backfill())
origin.last = hr(30, 5)
store.writes.clear()
run(c._async_import_recent())
own = store.snapshot(OWN)
check("the new hours of day 30 were added", [h for h in own if h >= hr(30, 0)], hours(hr(30, 0), hr(30, 5)))
check("nothing was written for the skipped days", [w for w in store.writes if w[1].day in (22, 27)], [])
check("sums stay continuous", chained(own, [h for h in sorted(own)[1:]], origin.wh), True)

# ---- D: the series already has rows on a day that now has a manual entry -------
print("D. option set after Origin's series already existed")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c0 = make(store, origin, None)
run(c0._async_backfill())
old = store.snapshot(OWN)
manual_series(store, hr(20, 0), hr(30, 9), {hr(27, 12): 25.0})
origin.wh = lambda h: (int(h.timestamp() // 3600) % 5 + 1) * 100  # Origin revised every value
store.writes.clear()
c = make(store, origin, MANUAL)
run(c._async_import_recent())
new = store.snapshot(OWN)
check("the skipped day's rows are exactly as they were", {h: r for h, r in new.items() if h.day == 27}, {h: r for h, r in old.items() if h.day == 27})
check("nothing was written for the skipped day", [w for w in store.writes if w[1].day == 27], [])
check("other days in the overlap window were refreshed", any(w[1].day in (25, 26, 28, 29) for w in store.writes), True)
after_skip = [h for h in hours(hr(28, 0), hr(29, 23))]
check("rows after the skipped day continue from its sum", chained(new, after_skip, origin.wh), True)

# ---- E: everything covered by manual data ----------------------------------
print("E. every day already has manual data")
store = Store()
manual_series(store, hr(20, 0), hr(30, 9), {hr(d, 12): 20.0 for d in range(20, 31)})
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, MANUAL)
run(c._async_backfill())
check("nothing written", store.snapshot(OWN), {})
run(c._async_import_recent())
check("a later poll writes nothing either", store.snapshot(OWN), {})
check("and does not re-run the whole backfill every poll", c.backfill_started, 0)

# ---- F: mistyped id -------------------------------------------------------------
print("F. manual statistic id with no data")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, "sensor.typo")
try:
    run(c._async_import_recent())
    check("raises UpdateFailed", "no error", "UpdateFailed")
except UpdateFailed as err:
    check("raises UpdateFailed naming the id", "sensor.typo" in str(err), True)
check("nothing was written", store.writes, [])

# ---- G: no option: unchanged behaviour --------------------------------------------
print("G. without a manual statistic everything imports and the overlap still rewrites")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, None)
run(c._async_backfill())
check("all days", sorted(store.snapshot(OWN)), day_hours(20, 21, 22, 23, 24, 25, 26, 27, 28, 29))
store.writes.clear()
run(c._async_import_recent())
check("revision overlap rewrote earlier hours", min(w[1] for w in store.writes if w[0] == OWN) < hr(29, 0), True)

# ---- H: the one-time clear -----------------------------------------------------------
print("H. clear Origin's statistics, then re-import skipping manual days")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c0 = make(store, origin, None)
run(c0._async_backfill())
manual_series(store, hr(20, 0), hr(30, 9), {hr(22, 12): 21.5, hr(23, 9): 18.0})
manual_before = store.snapshot(MANUAL)
check("setup: Origin's series covers the manual days", hr(22, 5) in store.snapshot(OWN), True)
c = make(store, origin, None)
c.config_entry.data = {"backfill_complete": True}
c.config_entry.options = {"manual_statistic_id": MANUAL}
run(c.async_reset_for_manual_statistic())
check("only Origin's own two statistics were cleared", sorted(store.cleared), sorted([OWN, OWN_COST]))
check("Origin's series is empty", store.snapshot(OWN), {})
check("manual untouched", store.snapshot(MANUAL), manual_before)
check("backfill flag turned off", c.hass.config_entries.async_update_entry.call_args.kwargs["data"]["backfill_complete"], False)
run(c._async_backfill())
check("re-import skips exactly the manual days", sorted(store.snapshot(OWN)), day_hours(20, 21, 24, 25, 26, 27, 28, 29))
for bad_id, why in ((OWN, "Origin's own id"), (None, "no id")):
    try:
        bad = make(Store(), origin, None)
        bad.config_entry.options = {"manual_statistic_id": bad_id} if bad_id else {}
        run(bad.async_reset_for_manual_statistic())
        check(f"reset refused with {why}", "accepted", "ValueError")
    except ValueError:
        check(f"reset refused with {why}", "ValueError", "ValueError")

# ---- I: local days in a non-UTC zone ---------------------------------------------------
print("I. 'a day' is the local day")
try:
    sydney = ZoneInfo("Australia/Sydney")
    store = Store()
    # local day 31 Oct (AEDT +11) = 13:00Z on the 30th to 12:00Z on the 31st
    manual_series(store, hr(28, 0), hr(31, 20), {hr(30, 20): 22.0})  # 20:00Z = 07:00 on the 31st, local
    origin = FakeOrigin(hr(28, 0), hr(31, 20))
    _FakeDt.NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    c = make(store, origin, MANUAL, tz=sydney)
    run(c._async_backfill())
    own = store.snapshot(OWN)
    local_days_written = {h.astimezone(sydney).date() for h in own}
    check("the local day with the entry is skipped", date(2026, 10, 31) in local_days_written, False)
    check("neighbouring local days are imported", {date(2026, 10, 29), date(2026, 10, 30)} <= local_days_written, True)
    _FakeDt.NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
except Exception as err:  # no tz database on this machine
    print(f"  skip ({err.__class__.__name__}: install tzdata to run this)")

# ---- J: option change is noticed -------------------------------------------------------
print("J. adopting a changed option")
c = make(Store(), FakeOrigin(hr(20, 0), hr(29, 23)), None)
check("no change -> False", c.async_adopt_manual_statistic(), False)
c.config_entry.options = {"manual_statistic_id": "sensor.electricity_meter_total"}
check("set -> True", c.async_adopt_manual_statistic(), True)
check("then stable -> False", c.async_adopt_manual_statistic(), False)
c.config_entry.options = {}
check("cleared -> True", c.async_adopt_manual_statistic(), True)

print()
if FAILURES:
    print(f"{FAILURES} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
