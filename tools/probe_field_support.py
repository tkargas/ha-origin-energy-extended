#!/usr/bin/env python3
"""
Find which usage fields the backend can actually resolve.

A 7-day HOURLY window succeeds when we ask for the fields the integration
currently uses, and fails with INTERNAL_SERVER_ERROR when we ask for the
full set the schema advertises. So some field is unresolvable for this
account rather than the range being wrong -- worth knowing exactly which,
because the schema marks several of them non-null.

Adds one field at a time to a known-good query and reports the first that
breaks it, then retests each in isolation.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_field_support.py
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

# Envelope-level fields, tried one at a time on top of an empty envelope.
ENVELOPE_FIELDS = [
    "timeUnit",
    "timezone",
    "startDate",
    "endDate",
    "lastReadingDate",
    "hasPrevious",
    "hasNext",
    "hasFeedIn",
    "hasMissingDataPoints",
    "hasMissingCosts",
    "unitsOfMeasure { totalConsumedEnergy }",
    "unitsOfMeasure { totalCost }",
    "unitsOfMeasure { totalFeedInEnergy totalEarnings usageCost supplyCost feedInEarned }",
]

# dataPoints-level fields, likewise.
POINT_FIELDS = [
    "endDate",
    "totalConsumedEnergy",
    "totalCost",
    "usageCost",
    "supplyCost",
    "type",
    "totalFeedInEnergy",
    "totalEarnings",
    "feedInEarned",
]


async def attempt(session, token, agreement_id, envelope, point, start, end):
    """Run one query shape; return (ok, detail)."""
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
        errs = payload["errors"]
        code = errs[0].get("extensions", {}).get("code", "?")
        return False, f"{code}: {errs[0].get('message', '')[:60]}"

    usage = payload["data"]["viewer"]["kraken"]["service"]["usage"]
    return True, f"{len(usage['dataPoints'])} points"


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
        active = next((s for s in svcs if s["active"]), svcs[0])
        agreement_id = active["agreementId"]

        # Anchor a day back: the earlier probe showed that window works.
        end = datetime.now(dt_timezone.utc) - timedelta(days=1)
        start = end - timedelta(days=7)

        print("=== baseline (fields the integration uses today) ===")
        ok, detail = await attempt(
            session, token, agreement_id, "", "totalConsumedEnergy totalCost", start, end
        )
        print(f"  {'OK  ' if ok else 'FAIL'} {detail}")

        print("\n=== envelope fields, one at a time ===")
        for field in ENVELOPE_FIELDS:
            ok, detail = await attempt(
                session, token, agreement_id, field, "totalConsumedEnergy", start, end
            )
            print(f"  {'OK  ' if ok else 'FAIL'} {field:56} {detail}")
            await asyncio.sleep(0.3)

        print("\n=== dataPoint fields, one at a time ===")
        for field in POINT_FIELDS:
            ok, detail = await attempt(
                session, token, agreement_id, "", f"totalConsumedEnergy {field}", start, end
            )
            print(f"  {'OK  ' if ok else 'FAIL'} {field:56} {detail}")
            await asyncio.sleep(0.3)

        print("\n=== end anchored at now (not now-1d), baseline fields ===")
        now = datetime.now(dt_timezone.utc)
        ok, detail = await attempt(
            session, token, agreement_id, "", "totalConsumedEnergy totalCost",
            now - timedelta(days=7), now,
        )
        print(f"  {'OK  ' if ok else 'FAIL'} {detail}")


if __name__ == "__main__":
    asyncio.run(main())
