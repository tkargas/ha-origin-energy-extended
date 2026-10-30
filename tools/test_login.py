#!/usr/bin/env python3
"""
Standalone test of the Origin Energy token + API code against the real
site, using the Home Assistant container's own Python environment (so
aiohttp/yarl are already available) but touching nothing in its actual
/config -- no config entry, no restart, no effect on the running
instance either way.

Needs ORIGIN_SESSION_COOKIE: the value of the `auth0` cookie for
id.originenergy.com.au, copied from a browser where you're signed in.
See docs/session-cookie.md for how to grab it. No password required --
Origin's password endpoint is behind Kasada bot defence and cannot be
scripted.

Usage:
    set -a; source origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/test_login.py
"""
import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    pick_service,
    pick_time_unit,
)
from origin_energy.auth import (
    OriginEnergyAuthError,
    OriginEnergySessionExpired,
    async_login_with_cookie,
    current_session_cookie,
)
from origin_energy.const import BROWSER_HEADERS


async def main() -> None:
    cookie = os.environ.get("ORIGIN_SESSION_COOKIE")
    if not cookie:
        # getpass so the cookie isn't echoed into your scrollback -- it is
        # a live session credential, as good as a password.
        cookie = getpass.getpass("auth0 session cookie value: ").strip()

    async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
        print("\nExchanging session cookie for an access token...")
        try:
            tokens = await async_login_with_cookie(session, cookie)
        except OriginEnergySessionExpired as err:
            print(f"SESSION EXPIRED: {err}")
            return
        except OriginEnergyAuthError as err:
            print(f"AUTH FAILED: {err}")
            return
        print(f"OK -- access_token valid for {tokens.expires_in}s")

        rolled = current_session_cookie(session)
        if rolled and rolled != cookie:
            print("Auth0 rolled the session cookie forward (expiry extended)")
        else:
            print("Session cookie unchanged this round")

        api = OriginEnergyApiClient(session, tokens.access_token)

        print("\nFetching electricity service details...")
        try:
            services = await api.async_get_services("ELECTRICITY")
        except OriginEnergyApiError as err:
            print(f"AccountDetailsQuery FAILED: {err}")
            return
        if not services:
            print("No electricity services found on this account")
            return
        service = pick_service(services)
        agreement_id = service["agreementId"]
        print(f"{len(services)} service(s); using the live one")
        print(f"agreementId={agreement_id} active={service.get('active')}")
        print(f"meterPoint.identifier={service.get('meterPoint', {}).get('identifier')}")
        timezone = (
            (service.get("property") or {}).get("address") or {}
        ).get("components", {}).get("timezone")
        print(f"timezone={timezone}")

        available = service.get("availableUsageTimeUnits")
        time_unit = pick_time_unit(available)
        print(f"availableUsageTimeUnits={available}")
        print(f"chosen time_unit={time_unit}")

        print(f"\nFetching last 2 days of usage at {time_unit} granularity...")
        end = datetime.now(dt_timezone.utc)
        start = end - timedelta(days=2)
        try:
            result = await api.async_get_usage(agreement_id, start, end, time_unit)
        except OriginEnergyApiError as err:
            print(f"AccountUsageQuery FAILED: {err}")
            return

        points = result.points
        print(
            f"units: {result.consumption_unit}/{result.cost_unit}, "
            f"lastReadingDate={result.last_reading_date}, "
            f"hasFeedIn={result.has_feed_in}, missing={result.has_missing_points}"
        )
        print(f"Got {len(points)} points (showing first and last 5):")
        shown = points[:5] + ([None] + points[-5:] if len(points) > 10 else points[5:])
        for p in shown:
            if p is None:
                print("  ...")
                continue
            kwh = p["totalConsumedEnergy"] / 1000 if p.get("totalConsumedEnergy") is not None else None
            cost = p["totalCost"] / 100 if p.get("totalCost") is not None else None
            print(f"  {p['startDate']}: {kwh} kWh, ${cost}")

        total = sum(
            p["totalConsumedEnergy"] for p in points
            if p.get("totalConsumedEnergy") is not None
        ) / 1000
        print(f"\nTotal across window: {total:.3f} kWh")


if __name__ == "__main__":
    asyncio.run(main())
