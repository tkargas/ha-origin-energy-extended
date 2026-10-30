#!/usr/bin/env python3
"""
Is the backend's INTERNAL_SERVER_ERROR about query size, or just flaky?

Every usage field resolves fine on its own, yet asking for all of them at
once failed. Two possibilities with opposite fixes: a query-complexity
ceiling (ask for less) or an intermittent backend fault (retry). Repeating
the same three query shapes tells them apart -- a size limit fails the big
shape every time, flakiness fails shapes at random.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_reliability.py
"""
import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS, GRAPHQL_URL

TEMPLATE = """
query Probe($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!) {
  viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
    ... on ElectricityService {
      usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
        %(envelope)s
        dataPoints { startDate %(point)s }
      }
    }
  } } }
}
"""

SHAPES = {
    "small (today's integration)": ("", "totalConsumedEnergy totalCost"),
    "medium (envelope + quality)": (
        "lastReadingDate hasFeedIn hasMissingDataPoints hasMissingCosts "
        "unitsOfMeasure { totalConsumedEnergy totalCost }",
        "endDate type totalConsumedEnergy totalCost",
    ),
    "large (everything)": (
        "timeUnit timezone startDate endDate lastReadingDate hasPrevious hasNext "
        "hasFeedIn hasMissingDataPoints hasMissingCosts "
        "unitsOfMeasure { totalConsumedEnergy totalFeedInEnergy totalCost "
        "totalEarnings usageCost supplyCost feedInEarned }",
        "endDate type totalConsumedEnergy totalFeedInEnergy totalCost "
        "usageCost supplyCost totalEarnings feedInEarned",
    ),
}

ROUNDS = 5


async def attempt(session, token, agreement_id, envelope, point, start, end):
    query = TEMPLATE % {"envelope": envelope, "point": point}
    try:
        async with session.post(
            GRAPHQL_URL,
            json={
                "query": query,
                "variables": {
                    "agreementId": agreement_id,
                    "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "endDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            payload = await resp.json()
    except Exception as err:  # noqa: BLE001
        return False, f"transport: {err}"

    if payload.get("errors"):
        err = payload["errors"][0]
        return False, err.get("extensions", {}).get("code", "?")
    usage = payload["data"]["viewer"]["kraken"]["service"]["usage"]
    return True, f"{len(usage['dataPoints'])}pts"


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
        token = tokens.access_token

        async with session.post(
            GRAPHQL_URL,
            json={
                "query": """{ viewer { kraken { services(type: ELECTRICITY) {
                     ... on ElectricityService { agreementId active } } } } }"""
            },
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            svcs = (await resp.json())["data"]["viewer"]["kraken"]["services"]
        agreement_id = next((s for s in svcs if s["active"]), svcs[0])["agreementId"]

        end = datetime.now(dt_timezone.utc) - timedelta(days=1)
        start = end - timedelta(days=7)

        tally: dict[str, list[str]] = {}
        for round_no in range(1, ROUNDS + 1):
            print(f"\n--- round {round_no} ---")
            for label, (envelope, point) in SHAPES.items():
                ok, detail = await attempt(
                    session, token, agreement_id, envelope, point, start, end
                )
                tally.setdefault(label, []).append("ok" if ok else detail)
                print(f"  {'OK  ' if ok else 'FAIL'} {label:30} {detail}")
                await asyncio.sleep(0.5)

        print("\n=== summary over %d rounds ===" % ROUNDS)
        for label, results in tally.items():
            wins = results.count("ok")
            print(f"  {label:30} {wins}/{len(results)} ok   {results}")


if __name__ == "__main__":
    asyncio.run(main())
