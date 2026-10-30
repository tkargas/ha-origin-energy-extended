#!/usr/bin/env python3
"""
Probe the usage envelope, data quality and how far back real data goes.

Three things the integration currently guesses at and shouldn't:

  * units -- we hard-code Wh->kWh and cents->dollars, but the API
    declares unitsOfMeasure per field, so we can check rather than assume.
  * data quality -- every point carries a UsageDataType (ACTUAL,
    APPROXIMATE, PARTIAL, PREDICTED, MISSING). Importing PREDICTED points
    would write invented numbers into Home Assistant's statistics.
  * history depth -- FIRST_RUN_HISTORY_DAYS is 730, but hasPrevious and
    the service's validFrom say where data really starts, so the backfill
    can be bounded instead of blindly requesting two years.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_usage_depth.py
"""
import asyncio
import getpass
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS, GRAPHQL_URL

# The full envelope, not just dataPoints -- everything the wrapper knows
# about the window it just answered.
ENVELOPE_QUERY = """
query Envelope($agreementId: String!, $timeUnit: UsageTimeUnit!, $startDate: DateTime!, $endDate: DateTime!, $filter: EnergyUsageFilter) {
  viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
    ... on ElectricityService {
      usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC, filter: $filter) {
        timeUnit timezone startDate endDate lastReadingDate
        hasPrevious hasNext hasFeedIn hasMissingDataPoints hasMissingCosts
        unitsOfMeasure {
          totalConsumedEnergy totalFeedInEnergy totalCost
          totalEarnings usageCost supplyCost feedInEarned
        }
        dataPoints {
          type startDate endDate
          totalConsumedEnergy totalFeedInEnergy
          totalCost usageCost supplyCost totalEarnings feedInEarned
        }
      }
    }
  } } }
}
"""


async def gql(session, token, query, variables=None, attempts=3):
    """Run a query, retrying the backend's intermittent 5xx-style errors.

    Repeating identical queries showed INTERNAL_SERVER_ERROR appearing at
    random rather than for any particular query shape, so a retry is the
    right response to it. Range errors are deterministic and re-raise at
    once.
    """
    for attempt in range(1, attempts + 1):
        async with session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            payload = await resp.json()

        if not payload.get("errors"):
            return payload["data"]

        detail = json.dumps(payload["errors"])[:400]
        if "Range is too large" in detail or attempt == attempts:
            raise RuntimeError(detail)
        await asyncio.sleep(2 * attempt)
    raise RuntimeError("unreachable")


async def usage(session, token, agreement_id, unit, start, end, filt=None):
    variables = {
        "agreementId": agreement_id,
        "timeUnit": unit,
        "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    # Never send filter: null -- the backend 500s on an explicit null but
    # is fine when the argument is simply absent.
    if filt is not None:
        variables["filter"] = filt

    data = await gql(session, token, ENVELOPE_QUERY, variables)
    return data["viewer"]["kraken"]["service"]["usage"]


def summarise(label, env):
    """Print the envelope plus a breakdown of point types and null-ness."""
    points = env["dataPoints"]
    types = Counter(p["type"] for p in points)
    nulls = Counter(
        field
        for p in points
        for field in ("totalConsumedEnergy", "totalCost", "totalFeedInEnergy")
        if p[field] is None
    )
    real = [p for p in points if p["totalConsumedEnergy"] is not None]
    print(f"\n--- {label} ---")
    print(
        f"  window {env['startDate']} .. {env['endDate']}\n"
        f"  lastReadingDate={env['lastReadingDate']} tz={env['timezone']}\n"
        f"  hasPrevious={env['hasPrevious']} hasNext={env['hasNext']} "
        f"hasFeedIn={env['hasFeedIn']} "
        f"missingPoints={env['hasMissingDataPoints']} missingCosts={env['hasMissingCosts']}"
    )
    print(f"  {len(points)} points, types={dict(types)}, nulls={dict(nulls)}")
    if real:
        print(f"  first real value: {real[0]['startDate']} -> {real[0]['totalConsumedEnergy']}")
        print(f"  last  real value: {real[-1]['startDate']} -> {real[-1]['totalConsumedEnergy']}")


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

        services = await gql(
            session,
            token,
            """{ viewer { kraken { services(type: ELECTRICITY) {
                 ... on ElectricityService { agreementId active validFrom validTo
                   serviceState usageReportJob { status progress downloadUrl } } } } } }""",
        )
        found = services["viewer"]["kraken"]["services"]
        print("=== services ===")
        for svc in found:
            print(
                f"  active={svc['active']} state={svc['serviceState']} "
                f"validFrom={svc['validFrom']} validTo={svc['validTo']} "
                f"reportJob={svc['usageReportJob']}"
            )
        # Pick the active one deliberately -- index 0 is not reliable.
        active = next((s for s in found if s["active"]), found[0])
        agreement_id = active["agreementId"]

        now = datetime.now(dt_timezone.utc)

        print("\n=== units + envelope, recent 7 days HOURLY ===")
        env = await usage(session, token, agreement_id, "HOURLY", now - timedelta(days=7), now)
        print("  unitsOfMeasure: " + json.dumps(env["unitsOfMeasure"]))
        summarise("HOURLY last 7d", env)
        sample = [p for p in env["dataPoints"] if p["totalConsumedEnergy"] is not None]
        if sample:
            print("  sample point: " + json.dumps(sample[-1]))

        print("\n=== does DAILY beyond move-in return nulls or nothing? ===")
        for days_back, span in ((1000, 30), (400, 30), (120, 30)):
            start = now - timedelta(days=days_back)
            env = await usage(session, token, agreement_id, "DAILY", start, start + timedelta(days=span))
            summarise(f"DAILY {days_back}d back", env)
            await asyncio.sleep(0.3)

        print("\n=== how far back does HOURLY go? ===")
        for days_back in (30, 60, 65, 70, 90, 180):
            start = now - timedelta(days=days_back)
            env = await usage(session, token, agreement_id, "HOURLY", start, start + timedelta(days=7))
            summarise(f"HOURLY {days_back}d back", env)
            await asyncio.sleep(0.3)

        print("\n=== EnergyUsageFilter: ACTUAL only ===")
        env = await usage(
            session, token, agreement_id, "HOURLY",
            now - timedelta(days=3), now, {"type_in": ["ACTUAL"]},
        )
        summarise("HOURLY 3d, type_in ACTUAL", env)

        print("\n=== trailing edge: does today return PREDICTED/PARTIAL? ===")
        env = await usage(session, token, agreement_id, "HOURLY", now - timedelta(days=2), now + timedelta(days=1))
        summarise("HOURLY now-2d .. now+1d", env)

        print("\n=== HALF_HOURLY at 9 days (day-limit vs read-limit) ===")
        try:
            env = await usage(
                session, token, agreement_id, "HALF_HOURLY",
                now - timedelta(days=9), now,
            )
            print(f"  OK {len(env['dataPoints'])} points -- limit counts days, not reads")
        except RuntimeError as err:
            print(f"  FAIL {err[:120] if isinstance(err, str) else str(err)[:120]}")

        print("\n=== historical rates (no active filter) ===")
        rates = await gql(
            session,
            token,
            """query R($agreementId: String!) { viewer { kraken {
                 service(type: ELECTRICITY, agreementId: $agreementId) {
                   ... on ElectricityService { product { rates {
                     description timeOfDay bandCategory rateIncludingTax
                     activeFrom activeTo active } } } } } } }""",
            {"agreementId": agreement_id},
        )
        for rate in rates["viewer"]["kraken"]["service"]["product"]["rates"]:
            print(
                f"  {rate['description']:14} {rate['timeOfDay']:9} "
                f"{rate['rateIncludingTax']:10.4f}  "
                f"{(rate['activeFrom'] or '')[:10]} -> {(rate['activeTo'] or 'current')[:10]}"
                f"  active={rate['active']}"
            )


if __name__ == "__main__":
    asyncio.run(main())
