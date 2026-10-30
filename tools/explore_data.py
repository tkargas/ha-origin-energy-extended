#!/usr/bin/env python3
"""
Inventory what the account actually returns, field by field.

The schema says what *can* be asked for; this says what's really
populated for this account, which is what decides whether a field is
worth building on. Identifiers and personal details are masked on the way
out so the output is safe to paste into an issue or a transcript.

Deliberately read-only: the schema also exposes mutations (payment
methods, refunds, move-out, self meter reads) and this never calls them.
Payment/bank/card fields aren't queried at all.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/explore_data.py
"""
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy.auth import OriginEnergyAuthError, async_login_with_cookie
from origin_energy.const import BROWSER_HEADERS, GRAPHQL_URL

# Field names whose values identify the account or the person. Masked
# rather than dropped so the shape stays visible.
SENSITIVE = {
    "accountNumber", "agreementId", "identifier", "serialNumber", "meterId",
    "meterPointId", "propertyId", "registerId", "number", "name", "firstName",
    "lastName", "email", "mobile", "landline", "dateOfBirth", "id", "billId",
    "krakenUserId", "accountId", "serviceId", "backendServiceId", "reference",
    "salesId", "portfolioId", "streetNumber", "streetName", "streetParts",
    "streetNameParts", "streetNumberParts", "unitNumber", "lotNumber",
    "buildingName", "latitude", "longitude", "dpNumber", "section", "block",
}


def mask(value: str) -> str:
    """Keep enough of an identifier to tell two apart, not enough to use."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def scrub(node, key: str | None = None):
    """Recursively mask sensitive leaf values."""
    if isinstance(node, dict):
        return {k: scrub(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [scrub(v, key) for v in node]
    if key in SENSITIVE and isinstance(node, (str, int, float)):
        return mask(str(node))
    return node


# Each entry is (label, query). Split into separate queries so one
# unsupported field can't blank out the whole report.
QUERIES: list[tuple[str, str]] = [
    ("gateway", "{ applicationInfo { name version environment } }"),
    (
        "user",
        """{ viewer { kraken { user { type krakenUserId name email mobile
             loyaltyCards { id } } } } }""",
    ),
    (
        "accounts",
        """{ viewer { kraken { accounts {
             accountNumber status accountType accountState isBusiness balance
             isInHardship hasHeldBill nextBillDate currentBillingPeriodStartDate
             hasCurrentCentrepay
             balanceInfo { fullBalance eligibleBalanceForRefund
                           eligibleBalanceForTransfer hasWrittenOffOrSoldDebt }
             dueCharges { amount dueDate }
             ledgers { number type }
             lifeSupport { __typename }
           } } } }""",
    ),
    (
        "bills",
        """{ viewer { kraken { accounts {
             accountNumber
             bills(first: 12) {
               pageInfo { hasNextPage hasPreviousPage }
               edges { node { billId billType fromDate toDate issuedDate
                              estimatedStatus isFinal paymentDueDate } }
             }
           } } } }""",
    ),
    (
        "properties",
        """{ viewer { kraken { properties {
             propertyId moveInDate moveOutDate
             address { components { locality postcode state stateName timezone } }
             ppor { __typename }
           } } } }""",
    ),
    ("concessions", "{ viewer { kraken { concessions { cardType cardLabel isCurrent isCardValid cardExpiryDate } } } }"),
    (
        "electricity_service",
        """{ viewer { kraken { services(type: ELECTRICITY) {
             ... on ElectricityService {
               agreementId type active validFrom validTo serviceState
               availableUsageTimeUnits
               product { code name description isStandingOffer type
                         rates(filter: {active_equal: true}) {
                           rateIncludingTax rateExcludingTax description timeOfDay
                           bandCategory networkTariffCode chargeDescription
                           stepStart stepEnd activeFrom activeTo active } }
               meterPoint { identifier meterPointType supplyStatus
                 serviceProvider { name emergencyPhoneNumber }
                 meters { serialNumber meterType installationType status active
                          activeFrom activeTo nextScheduledReadDate
                          registers { identifier timeOfDay networkTariffCode
                                      typeOfMeasure energyDirection status
                                      numberOfDials } } }
               property { propertyId address { components { locality postcode state timezone } } }
             } } } } }""",
    ),
    (
        "other_service_types",
        """{ viewer { kraken {
             gas: services(type: GAS) { __typename }
             hotWater: services(type: EMBEDDED_HOT_WATER) { __typename }
             embedded: services(type: EMBEDDED_ELECTRICITY) { __typename }
           } } }""",
    ),
    (
        "digital",
        """{ viewer { digital {
             user { __typename }
             accounts { accountId accountType systemType status
                        services { serviceId type status reference } }
             services { serviceId type status }
             features { __typename }
           } } }""",
    ),
]


async def run(session, token, label, query) -> None:
    async with session.post(
        GRAPHQL_URL,
        json={"query": query},
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        payload = await resp.json()

    print(f"\n{'=' * 62}\n=== {label}  (HTTP {resp.status})\n{'=' * 62}")
    if payload.get("errors"):
        print("ERRORS: " + json.dumps(payload["errors"])[:1200])
    if payload.get("data"):
        print(json.dumps(scrub(payload["data"]), indent=2, default=str)[:6000])


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

        for label, query in QUERIES:
            try:
                await run(session, tokens.access_token, label, query)
            except Exception as err:  # noqa: BLE001 - keep going through the list
                print(f"\n=== {label}: REQUEST FAILED: {err}")
            await asyncio.sleep(0.3)


if __name__ == "__main__":
    asyncio.run(main())
