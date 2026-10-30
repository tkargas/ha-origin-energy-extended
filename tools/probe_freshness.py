#!/usr/bin/env python3
"""
What time of day does Origin actually publish new usage data?

The integration polls every 24 hours from whenever it last started, so a
restart at midday pins every future update to midday. That is only
harmless if data lands at a time of day nobody cares about -- and nothing
in the API documents when that is. `lastReadingDate` is the backend's own
freshness watermark, so watching it across a full day and noting the
wall-clock moment it jumps answers the question by observation instead of
by assumption.

Run it for at least ~26 hours so a daily publication cannot hide in the
gap between the start and end of the window. Each poll appends a row to a
CSV, so an interrupted run keeps everything it saw and a second run can
extend the same log.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_freshness.py

Options:
    --interval-minutes N   how often to poll (default 20)
    --hours H              how long to keep watching (default 30)
    --log PATH             CSV to append to (default ./freshness_log.csv)
    --summarise-only       just re-read the CSV and print the advances
"""
import argparse
import asyncio
import csv
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    parse_api_datetime,
    pick_service,
    pick_time_unit,
)
from origin_energy.auth import (
    OriginEnergyAuthError,
    async_get_tokens,
    seed_session_cookie,
)
from origin_energy.const import BROWSER_HEADERS, DEFAULT_TIMEZONE

# A window this short is cheap and still comfortably spans the watermark:
# lastReadingDate sits around yesterday, so three days back always
# contains it plus room for it to move forward.
LOOKBACK_DAYS = 3

# Tokens last an hour and this runs for a day or more, so re-mint well
# before expiry rather than letting a 401 end the watch.
TOKEN_MARGIN = timedelta(minutes=5)

FIELDS = ("polled_at_local", "polled_at_utc", "last_reading_date", "last_actual_start",
          "actual_points", "note")


class Watcher:
    """Holds the session and keeps a valid token across a multi-day run."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token: str | None = None
        self._expires_at: datetime | None = None

    async def client(self) -> OriginEnergyApiClient:
        now = datetime.now(dt_timezone.utc)
        if (
            self._token is None
            or self._expires_at is None
            or self._expires_at - now <= TOKEN_MARGIN
        ):
            tokens = await async_get_tokens(self._session)
            self._token = tokens.access_token
            self._expires_at = now + timedelta(seconds=tokens.expires_in)
        return OriginEnergyApiClient(self._session, self._token)


def _last_actual(points: list[dict]) -> tuple[datetime | None, int]:
    """Latest point carrying real consumption, and how many there are.

    MISSING buckets pad the window out past the watermark with null
    values, so counting points is meaningless -- only the ones with a
    reading say how far real data extends.
    """
    starts = [
        parse_api_datetime(p.get("startDate"))
        for p in points
        if p.get("totalConsumedEnergy") is not None
    ]
    starts = [s for s in starts if s is not None]
    return (max(starts) if starts else None, len(starts))


async def poll(watcher: Watcher, agreement_id: str, time_unit: str, tz: ZoneInfo) -> dict:
    now = datetime.now(tz)
    row = {
        "polled_at_local": now.isoformat(timespec="seconds"),
        "polled_at_utc": now.astimezone(dt_timezone.utc).isoformat(timespec="seconds"),
        "last_reading_date": "",
        "last_actual_start": "",
        "actual_points": "",
        "note": "",
    }
    try:
        api = await watcher.client()
        result = await api.async_get_usage(
            agreement_id, now - timedelta(days=LOOKBACK_DAYS), now, time_unit
        )
    except (OriginEnergyApiError, OriginEnergyAuthError, aiohttp.ClientError,
            TimeoutError) as err:
        # A single failed poll must not end a 30-hour watch; record it and
        # carry on, so a gap in the log is visibly a gap and not silence.
        row["note"] = f"error: {err}"
        return row

    last_actual, count = _last_actual(result.points)
    if result.last_reading_date is not None:
        row["last_reading_date"] = result.last_reading_date.isoformat()
    if last_actual is not None:
        row["last_actual_start"] = last_actual.isoformat()
    row["actual_points"] = str(count)
    if result.has_missing_points:
        row["note"] = "hasMissingDataPoints"
    return row


def summarise(log_path: Path, tz: ZoneInfo) -> None:
    """Print every point in the log where the watermark moved forward."""
    if not log_path.exists():
        print(f"no log at {log_path}")
        return

    with log_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        print("log is empty")
        return

    print(f"\n=== {len(rows)} polls in {log_path} ===")
    advances = []
    previous = None
    for row in rows:
        watermark = row.get("last_reading_date") or ""
        if not watermark:
            continue
        if previous is not None and watermark != previous:
            advances.append((row, previous))
        previous = watermark

    if not advances:
        print("watermark never moved during the watch -- run for longer, or the")
        print("publication happened before the first poll.")
        return

    for row, was in advances:
        seen = datetime.fromisoformat(row["polled_at_local"])
        now_utc = parse_api_datetime(row["last_reading_date"])
        was_utc = parse_api_datetime(was)
        gained = ""
        if now_utc and was_utc:
            gained = f", +{(now_utc - was_utc).total_seconds() / 3600:.1f}h of data"
        print(
            f"  {seen.strftime('%a %H:%M %Z')}  lastReadingDate {was} -> "
            f"{row['last_reading_date']}{gained}"
        )

    print(
        "\nThe advance is first *seen* at the poll above, so the real "
        f"publication falls in the preceding poll interval.\nSchedule updates "
        "shortly after the latest time seen here."
    )


async def watch(args: argparse.Namespace) -> None:
    cookie = os.environ.get("ORIGIN_SESSION_COOKIE")
    if not cookie:
        if not sys.stdin.isatty():
            # Detached (docker exec -d) there is nobody to answer a prompt,
            # and a 30-hour watch that silently blocked on one would look
            # exactly like a watch that found nothing.
            print("ORIGIN_SESSION_COOKIE is not set and there is no terminal to "
                  "prompt on -- pass it with `docker exec -e ORIGIN_SESSION_COOKIE`.")
            return
        cookie = getpass.getpass("auth0 session cookie value: ").strip()

    log_path = Path(args.log)
    async with aiohttp.ClientSession() as session:
        seed_session_cookie(session, cookie)
        watcher = Watcher(session)
        try:
            api = await watcher.client()
            services = await api.async_get_services()
        except OriginEnergyAuthError as err:
            print(f"AUTH FAILED: {err}")
            return

        service = pick_service(services)
        if service is None:
            print("no electricity service on this account")
            return
        agreement_id = service["agreementId"]
        time_unit = pick_time_unit(service.get("availableUsageTimeUnits"))
        address = (service.get("property") or {}).get("address") or {}
        tz_name = (address.get("components") or {}).get("timezone") or DEFAULT_TIMEZONE
        tz = ZoneInfo(tz_name)

        print(f"watching {time_unit} usage, timezone {tz_name}")
        if args.once:
            print("single poll\n")
        else:
            print(f"polling every {args.interval_minutes} min for {args.hours} h")
        print(f"appending to {log_path}\n")

        is_new = not log_path.exists() or log_path.stat().st_size == 0
        deadline = datetime.now(dt_timezone.utc) + timedelta(hours=args.hours)
        previous = None

        with log_path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if is_new:
                writer.writeheader()

            # Poll first, then decide whether to continue: a --once run has
            # to produce one observation, and a timed run should not end on
            # a pointless trailing sleep.
            while True:
                row = await poll(watcher, agreement_id, time_unit, tz)
                writer.writerow(row)
                handle.flush()  # a run this long is usually read while live

                watermark = row["last_reading_date"] or "-"
                marker = ""
                if previous is not None and watermark != previous and row["last_reading_date"]:
                    marker = "  <-- ADVANCED"
                previous = watermark if row["last_reading_date"] else previous
                print(
                    f"{row['polled_at_local']}  lastReadingDate={watermark}  "
                    f"lastActual={row['last_actual_start'] or '-'}  "
                    f"actual={row['actual_points'] or '-'}"
                    f"{'  ' + row['note'] if row['note'] else ''}{marker}"
                )

                if args.once or datetime.now(dt_timezone.utc) >= deadline:
                    break
                await asyncio.sleep(args.interval_minutes * 60)

    if not args.once:
        summarise(log_path, tz)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-minutes", type=int, default=20)
    parser.add_argument("--hours", type=float, default=30)
    parser.add_argument("--log", default="freshness_log.csv")
    parser.add_argument("--summarise-only", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll a single time and exit -- 'where is the watermark right now?'",
    )
    args = parser.parse_args()

    if args.once:
        args.hours = 0.0
        args.interval_minutes = 0

    if args.summarise_only:
        summarise(Path(args.log), ZoneInfo(DEFAULT_TIMEZONE))
        return
    asyncio.run(watch(args))


if __name__ == "__main__":
    main()
