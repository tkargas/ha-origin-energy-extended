#!/usr/bin/env python3
"""
Cross-check the API numbers against Origin's own CSV export.

The CSV is an independent source of truth for the same meter, so summing
its 30-minute rows per day and comparing against what the API reports
validates the whole chain at once: unit conversion (Wh -> kWh), timezone
and day-boundary handling, and that HOURLY buckets add up to the same
totals as DAILY.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker cp A-*-ELECTRICITY-*.csv homeassistant:/tmp/origin_energy_test/usage.csv
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/verify_against_csv.py \
        /tmp/origin_energy_test/usage.csv
"""
import asyncio
import csv
import getpass
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    chunk_days,
    pick_service,
)
from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS

TOLERANCE_KWH = 0.01


async def fetch_chunked(
    api: OriginEnergyApiClient,
    agreement_id: str,
    start: datetime,
    end: datetime,
    time_unit: str,
) -> list[dict]:
    """Fetch a range in gateway-sized windows, mirroring the coordinator."""
    by_start: dict[str, dict] = {}
    window = timedelta(days=chunk_days(time_unit))
    window_start = start
    while window_start < end:
        window_end = min(window_start + window, end)
        result = await api.async_get_usage(
            agreement_id, window_start, window_end, time_unit
        )
        for point in result.points:
            by_start[point["startDate"]] = point
        window_start = window_end
    return list(by_start.values())


def load_csv(path: str) -> tuple[dict[str, float], datetime, datetime]:
    """Sum the CSV's interval rows into per-day kWh totals."""
    by_day: dict[str, float] = defaultdict(float)
    starts: list[datetime] = []
    widths: set[int] = set()

    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            start = datetime.fromisoformat(row["From (date/time)"])
            end = datetime.fromisoformat(row["To (date/time)"])
            starts.append(start)
            widths.add(round((end - start).total_seconds() / 60))
            # Group by the local calendar date the interval starts in --
            # the same convention Origin's DAILY totals use.
            by_day[start.date().isoformat()] += float(row["Amount Used"])

    print(f"CSV: {len(starts)} rows, interval widths (min): {sorted(widths)}")
    print(f"CSV: {min(starts).isoformat()} .. {max(starts).isoformat()}")
    return dict(by_day), min(starts), max(starts)


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    csv_daily, csv_start, csv_end = load_csv(sys.argv[1])

    cookie = os.environ.get("ORIGIN_SESSION_COOKIE") or getpass.getpass(
        "auth0 session cookie value: "
    ).strip()

    async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
        try:
            tokens = await async_login_with_cookie(session, cookie)
        except OriginEnergyAuthError as err:
            print(f"AUTH FAILED: {err}")
            return
        api = OriginEnergyApiClient(session, tokens.access_token)

        services = await api.async_get_services("ELECTRICITY")
        if not services:
            print("No electricity service found")
            return
        # Same selection the integration makes: an account can list a
        # closed agreement alongside the live one.
        agreement_id = pick_service(services)["agreementId"]

        # Widen by a day each side so boundary days are fully covered.
        start = csv_start - timedelta(days=1)
        end = csv_end + timedelta(days=1)

        try:
            daily = await fetch_chunked(api, agreement_id, start, end, "DAILY")
            hourly = await fetch_chunked(api, agreement_id, start, end, "HOURLY")
        except OriginEnergyApiError as err:
            print(f"API FAILED: {err}")
            return

        api_daily = {
            p["startDate"][:10]: p["totalConsumedEnergy"] / 1000
            for p in daily
            if p.get("totalConsumedEnergy") is not None
        }
        # Roll HOURLY up to days the same way the coordinator will, to prove
        # the finer buckets reconcile with the daily totals.
        api_hourly_rolled: dict[str, float] = defaultdict(float)
        for p in hourly:
            if p.get("totalConsumedEnergy") is not None:
                api_hourly_rolled[p["startDate"][:10]] += p["totalConsumedEnergy"] / 1000

        print(f"\nAPI: {len(daily)} daily points, {len(hourly)} hourly points")
        print(f"\n{'date':12} {'CSV':>10} {'API daily':>10} {'API hourly':>11}  {'':4}")
        print("-" * 52)

        mismatches = 0
        for day in sorted(csv_daily):
            c = csv_daily[day]
            d = api_daily.get(day)
            h = api_hourly_rolled.get(day)
            if d is None and h is None:
                continue
            worst = max(
                abs(c - v) for v in (d, h) if v is not None
            )
            flag = "OK" if worst <= TOLERANCE_KWH else "DIFF"
            if flag == "DIFF":
                mismatches += 1
            print(
                f"{day:12} {c:10.3f} "
                f"{d if d is not None else float('nan'):10.3f} "
                f"{h if h is not None else float('nan'):11.3f}  {flag:4}"
            )

        print("-" * 52)
        compared = sum(
            1 for day in csv_daily if day in api_daily or day in api_hourly_rolled
        )
        print(f"{compared} days compared, {mismatches} outside +/-{TOLERANCE_KWH} kWh")
        if mismatches == 0:
            print(
                "\nAPI matches Origin's own export: unit conversion, day "
                "boundaries and HOURLY->daily rollup all reconcile."
            )


if __name__ == "__main__":
    asyncio.run(main())
