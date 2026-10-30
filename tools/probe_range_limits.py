#!/usr/bin/env python3
"""
Find the largest usage window Origin will actually serve, per granularity.

The gateway advertises a 1000-data-point ceiling, but rejecting a 37-day
HOURLY query (888 points) with EXTERNAL_API_ERROR shows the backend gives
up well before that. This walks window sizes downward and reports which
ones really work, so the coordinator's chunk size is measured rather than
assumed.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_range_limits.py
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

# Descending so the first success is the largest usable window.
CANDIDATES = {
    "HOURLY": [37, 30, 21, 14, 10, 7, 5, 3, 2],
    "DAILY": [900, 730, 365, 180, 90, 60, 30],
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
        # Collapse the GraphQL error blob down to something readable.
        for marker in ("Range is too large", "unexpected error", "timed out"):
            if marker in text:
                return False, f"{marker} ({elapsed:.1f}s)"
        return False, f"{text[:70]} ({elapsed:.1f}s)"
    return True, f"{len(points):5d} points ({time.monotonic() - began:.1f}s)"


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

        # Anchor a day back so the trailing edge is settled data.
        end = datetime.now(dt_timezone.utc) - timedelta(days=1)
        largest: dict[str, int | None] = {}

        for time_unit, sizes in CANDIDATES.items():
            print(f"\n=== {time_unit} ===")
            largest[time_unit] = None
            for days in sizes:
                ok, detail = await probe(api, agreement_id, time_unit, days, end)
                print(f"  {days:4d} days: {'OK  ' if ok else 'FAIL'} {detail}")
                if ok:
                    largest[time_unit] = days
                    break
                # Be gentle on a backend that just errored.
                await asyncio.sleep(1)

        print("\n=== largest working window ===")
        for time_unit, days in largest.items():
            if days is None:
                print(f"  {time_unit:12} no tested window succeeded")
            else:
                print(f"  {time_unit:12} {days} days")


if __name__ == "__main__":
    asyncio.run(main())
