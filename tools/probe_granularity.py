#!/usr/bin/env python3
"""
Find out how granular Origin's usage data can get.

Asks the GraphQL gateway three things:
  1. What values does the UsageTimeUnit enum actually accept?
  2. Does ElectricityService expose any interval/measurement field beyond
     the daily `usage` one we already use?
  3. For each accepted time unit, how many data points come back for a
     recent 2-day window -- i.e. what resolution do we really get?

Read-only: every call is a GraphQL query, nothing is mutated.

Usage:
    set -a; source origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_granularity.py
"""
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.api import ACCOUNT_USAGE_QUERY, OriginEnergyApiClient, OriginEnergyApiError
from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS

ENUM_INTROSPECTION = """
query { __type(name: "UsageTimeUnit") { enumValues { name } } }
"""

SERVICE_INTROSPECTION = """
query {
  __type(name: "ElectricityService") {
    fields { name args { name type { name kind ofType { name } } } }
  }
}
"""

DATAPOINT_INTROSPECTION = """
query { __type(name: "%s") { fields { name type { name kind ofType { name } } } } }
"""


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

        # --- 1. the enum -------------------------------------------------
        units: list[str] = []
        print("=== UsageTimeUnit enum values ===")
        try:
            data = await api._graphql(ENUM_INTROSPECTION, {})
            t = data.get("__type")
            if t:
                units = [v["name"] for v in t["enumValues"]]
                print("  " + ", ".join(units))
        except OriginEnergyApiError as err:
            print(f"  introspection blocked ({str(err)[:120]})")

        if not units:
            # Introspection disabled? Send a deliberately invalid enum value
            # -- GraphQL validation errors usually list the legal ones.
            print("  probing via invalid-value error message...")
            try:
                await api._graphql(
                    ACCOUNT_USAGE_QUERY,
                    {
                        "agreementId": "0",
                        "type": "ELECTRICITY",
                        "timeUnit": "NOT_A_REAL_UNIT",
                        "startDate": "2026-07-01T00:00:00.000Z",
                        "endDate": "2026-07-02T00:00:00.000Z",
                    },
                )
            except OriginEnergyApiError as err:
                print(f"  {str(err)[:600]}")
            units = ["DAILY", "BILLING", "MONTHLY", "WEEKLY", "HOURLY", "HALF_HOURLY", "INTERVAL"]
            print(f"  falling back to trying: {', '.join(units)}")

        # --- 2. other fields on the service ------------------------------
        print("\n=== ElectricityService fields (looking for interval data) ===")
        try:
            data = await api._graphql(SERVICE_INTROSPECTION, {})
            t = data.get("__type")
            if t:
                for f in t["fields"]:
                    args = ", ".join(a["name"] for a in f["args"])
                    print(f"  {f['name']}({args})" if args else f"  {f['name']}")
        except OriginEnergyApiError as err:
            print(f"  introspection blocked ({str(err)[:120]})")

        # --- 3. what each unit actually returns --------------------------
        print("\n=== data points returned per time unit (recent 2-day window) ===")
        end = datetime.now(dt_timezone.utc) - timedelta(days=1)
        start = end - timedelta(days=2)
        agreement_id = None
        services = await api.async_get_services("ELECTRICITY")
        if services:
            agreement_id = services[0]["agreementId"]
        if not agreement_id:
            print("  no service found, skipping")
            return

        for unit in units:
            try:
                data = await api._graphql(
                    ACCOUNT_USAGE_QUERY,
                    {
                        "agreementId": agreement_id,
                        "type": "ELECTRICITY",
                        "timeUnit": unit,
                        "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "endDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    },
                )
                pts = (
                    ((data.get("viewer") or {}).get("kraken") or {}).get("service") or {}
                ).get("usage", {}).get("dataPoints") or []
                print(f"  {unit:14} -> {len(pts)} points", end="")
                if pts:
                    print(f"   first={pts[0]['startDate']} .. {pts[0]['endDate']}")
                    if len(pts) > 1:
                        print(f"  {'':14}    second={pts[1]['startDate']}")
                else:
                    print()
            except OriginEnergyApiError as err:
                print(f"  {unit:14} -> rejected: {str(err)[:110]}")


if __name__ == "__main__":
    asyncio.run(main())
