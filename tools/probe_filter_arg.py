#!/usr/bin/env python3
"""
Work out why an otherwise-identical usage query fails.

The full-field query fails consistently when written with $timeUnit and
$filter as variables, yet the same field set succeeds with both inlined.
GraphQL distinguishes "argument omitted" from "argument passed as null",
so a resolver checking one and not the other would explain it. These
variants isolate which of the two is responsible.

Usage:
    set -a; source tools/origin_secrets.env; set +a
    docker exec -i -e ORIGIN_SESSION_COOKIE \
        homeassistant python3 /tmp/origin_energy_test/probe_filter_arg.py
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

POINTS = "dataPoints { startDate totalConsumedEnergy }"

# (label, query, extra variables)
VARIANTS = [
    (
        "inline timeUnit, no filter arg",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC) { %s } } } } } }""" % POINTS,
        {},
    ),
    (
        "variable timeUnit, no filter arg",
        """query Q($agreementId: String!, $timeUnit: UsageTimeUnit!, $startDate: DateTime!, $endDate: DateTime!) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC) { %s } } } } } }""" % POINTS,
        {"timeUnit": "HOURLY"},
    ),
    (
        "inline filter: null",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC, filter: null) { %s } } } } } }""" % POINTS,
        {},
    ),
    (
        "variable filter, passed null",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!, $filter: EnergyUsageFilter) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC, filter: $filter) { %s } } } } } }""" % POINTS,
        {"filter": None},
    ),
    (
        "variable filter, omitted from variables",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!, $filter: EnergyUsageFilter) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC, filter: $filter) { %s } } } } } }""" % POINTS,
        {},
    ),
    (
        "inline filter: {type_in: [ACTUAL]}",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC, filter: {type_in: [ACTUAL]}) { %s } } } } } }""" % POINTS,
        {},
    ),
    (
        "variable filter, passed {type_in: [ACTUAL]}",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!, $filter: EnergyUsageFilter) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate,
                       orderBy: START_DATE_ASC, filter: $filter) { %s } } } } } }""" % POINTS,
        {"filter": {"type_in": ["ACTUAL"]}},
    ),
    (
        "no orderBy at all",
        """query Q($agreementId: String!, $startDate: DateTime!, $endDate: DateTime!) {
             viewer { kraken { service(type: ELECTRICITY, agreementId: $agreementId) {
               ... on ElectricityService {
                 usage(timeUnit: HOURLY, startDate: $startDate, endDate: $endDate) { %s } } } } } }""" % POINTS,
        {},
    ),
]


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
        base = {
            "agreementId": agreement_id,
            "startDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }

        for label, query, extra in VARIANTS:
            async with session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": {**base, **extra}},
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                payload = await resp.json()

            if payload.get("errors"):
                err = payload["errors"][0]
                code = err.get("extensions", {}).get("code", "?")
                print(f"  FAIL {label:44} {code}: {err.get('message', '')[:50]}")
            else:
                usage = payload["data"]["viewer"]["kraken"]["service"]["usage"]
                pts = usage["dataPoints"]
                print(f"  OK   {label:44} {len(pts)} points")
            await asyncio.sleep(0.4)


if __name__ == "__main__":
    asyncio.run(main())
