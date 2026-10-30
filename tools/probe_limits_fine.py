#!/usr/bin/env python3
"""
Pin down what Origin's sub-daily range limit actually counts.

The coarse probe showed HOURLY dies between 7 and 10 days while DAILY
happily returns 900 points, so the documented "1000 data points" ceiling
is not the binding constraint. Two candidate explanations:

  * a limit on elapsed days, in which case HALF_HOURLY also survives ~7
    days (336 points), or
  * a limit on underlying half-hourly meter reads, in which case
    HALF_HOURLY dies at roughly half the HOURLY window.

Testing both granularities over the same window sizes separates them.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_limits_fine.py
"""
import asyncio
import getpass
import os
import sys
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.api import OriginEnergyApiClient, OriginEnergyApiError
from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS

# Ascending here: we want the exact step where each granularity breaks,
# not just the first size that happens to work.
PLAN = {
    "HALF_HOURLY": [1, 2, 3, 4, 5, 6, 7, 8, 10, 14],
    "HOURLY": [7, 8, 9, 10, 11, 12],
    "DAILY": [900, 950, 990, 999, 1000, 1200],
    "MONTHLY": [365, 730, 900],
    "BILLING": [365, 730],
}


async def probe(api, agreement_id, time_unit, days, end):
    """Return (ok, detail) for one window size, timing the request."""
    start = end - timedelta(days=days)
    began = time.monotonic()
    try:
        points = (await api.async_get_usage(agreement_id, start, end, time_unit)).points
    except OriginEnergyApiError as err:
        elapsed = time.monotonic() - began
        text = str(err)
        for marker in ("Range is too large", "unexpected error", "timed out"):
            if marker in text:
                return False, f"{marker} ({elapsed:.1f}s)"
        return False, f"{text[:90]} ({elapsed:.1f}s)"

    elapsed = time.monotonic() - began
    if not points:
        return True, f"    0 points -- EMPTY ({elapsed:.1f}s)"

    # Interval width tells us whether the granularity is honoured or
    # silently downgraded to something coarser.
    first, last = points[0], points[-1]
    width = ""
    try:
        started = datetime.fromisoformat(first["startDate"])
        ended = datetime.fromisoformat(first["endDate"])
        width = f", bucket {int((ended - started).total_seconds() / 60)}min"
    except (KeyError, TypeError, ValueError):
        pass
    return True, (
        f"{len(points):5d} points ({elapsed:.1f}s){width}"
        f"\n            {first.get('startDate')} .. {last.get('endDate')}"
    )


async def main() -> None:
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
        agreement_id = services[0]["agreementId"]
        print(f"availableUsageTimeUnits: {services[0].get('availableUsageTimeUnits')}")

        end = datetime.now(dt_timezone.utc) - timedelta(days=1)

        for time_unit, sizes in PLAN.items():
            print(f"\n=== {time_unit} ===")
            for days in sizes:
                ok, detail = await probe(api, agreement_id, time_unit, days, end)
                print(f"  {days:5d} days: {'OK  ' if ok else 'FAIL'} {detail}")
                await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
