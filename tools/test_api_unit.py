#!/usr/bin/env python3
"""
Offline checks for the API client's decision-making.

These are the bits that are easy to get subtly wrong and hard to notice
in production: which agreement gets picked when an account has more than
one, and which failures are retried versus surfaced. No network involved
-- the session is faked -- so this runs anywhere the package imports.

Usage:
    docker exec -i homeassistant python3 /tmp/origin_energy_test/test_api_unit.py
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from origin_energy import api as api_module
from origin_energy.const import BROWSER_HEADERS
from origin_energy.api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    chunk_days,
    format_api_datetime,
    pick_service,
    pick_time_unit,
)

# Retries are exercised for their branching, not their timing.
api_module.RETRY_BACKOFF_SECONDS = 0

FAILURES = 0


def check(label: str, got, want) -> None:
    global FAILURES
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


class _FakeCtx:
    """Stands in for aiohttp's request context manager."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeSession:
    """Replays a scripted list of responses, counting the attempts."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        self.sent = []
        self.headers_sent = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.sent.append(json)
        self.headers_sent.append(headers or {})
        return _FakeCtx(self._results.pop(0))


def graphql_error(code):
    return _FakeResponse(200, {"errors": [{"message": "boom", "extensions": {"code": code}}]})


def usage_ok(points=1):
    return _FakeResponse(
        200,
        {
            "data": {
                "viewer": {
                    "kraken": {
                        "service": {
                            "usage": {
                                "lastReadingDate": "2026-07-22T14:00:00.000Z",
                                "hasFeedIn": False,
                                "hasMissingDataPoints": True,
                                "unitsOfMeasure": {
                                    "totalConsumedEnergy": "WATT_HOUR",
                                    "totalCost": "CENT",
                                },
                                "dataPoints": [
                                    {
                                        "type": "ACTUAL",
                                        "startDate": "2026-07-22T23:00:00.000+10:00",
                                        "totalConsumedEnergy": 493,
                                        "totalCost": 22.143,
                                    }
                                ]
                                * points,
                            }
                        }
                    }
                }
            }
        },
    )


async def fetch(session):
    client = OriginEnergyApiClient(session, "token")
    now = datetime.now(dt_timezone.utc)
    return await client.async_get_usage("A-1", now - timedelta(days=1), now)


async def main() -> None:
    print("pick_service")
    live = {"agreementId": "live", "active": True, "validFrom": "2026-05-19T14:00:00.000Z"}
    closed = {"agreementId": "old", "active": False, "validFrom": "2026-05-19T14:00:00.000Z"}
    # The defunct agreement listed first is exactly the case services[0] got wrong.
    check("prefers the active agreement", pick_service([closed, live])["agreementId"], "live")
    check("empty list yields None", pick_service([]), None)
    check(
        "pins to a configured agreement",
        pick_service([closed, live], "old")["agreementId"],
        "old",
    )
    check(
        "unknown agreement falls back to active",
        pick_service([closed, live], "gone")["agreementId"],
        "live",
    )
    older = {"agreementId": "older", "active": False, "validFrom": "2020-01-01T00:00:00.000Z"}
    check(
        "all-inactive picks most recent",
        pick_service([older, closed])["agreementId"],
        "old",
    )

    print("\nchunk_days")
    check("hourly stays under the 9-day cap", chunk_days("HOURLY"), 7)
    check("half-hourly matches hourly", chunk_days("HALF_HOURLY"), 7)
    check("daily uses the point ceiling", chunk_days("DAILY"), 900)
    check("unknown unit is conservative", chunk_days("WEEKLY"), 7)

    print("\npick_time_unit")
    check("prefers hourly", pick_time_unit(["BILLING", "DAILY", "HOURLY"]), "HOURLY")
    check("falls back to daily", pick_time_unit(["BILLING", "DAILY"]), "DAILY")
    check("handles a missing list", pick_time_unit(None), "DAILY")

    print("\nformat_api_datetime")
    # The gateway's DateTime scalar is given a literal trailing Z, so the
    # components have to be UTC. Formatting a local-zone instant directly
    # would shift the whole query window by the offset.
    sydney = dt_timezone(timedelta(hours=10))
    check(
        "converts a +10:00 instant to UTC",
        format_api_datetime(datetime(2026, 7, 23, 9, 0, tzinfo=sydney)),
        "2026-07-22T23:00:00.000Z",
    )
    check(
        "leaves a UTC instant alone",
        format_api_datetime(datetime(2026, 7, 23, 9, 0, tzinfo=dt_timezone.utc)),
        "2026-07-23T09:00:00.000Z",
    )

    print("\nquery construction")
    session = _FakeSession([usage_ok()])
    client = OriginEnergyApiClient(session, "token")
    await client.async_get_usage(
        "A-1",
        datetime(2026, 7, 20, 0, 0, tzinfo=sydney),
        datetime(2026, 7, 27, 0, 0, tzinfo=sydney),
    )
    sent = session.sent[0]
    check("start is sent as UTC", sent["variables"]["startDate"], "2026-07-19T14:00:00.000Z")
    # An explicit filter: null is the one input that reliably 500s this
    # backend, and it is exactly what a client sends for an unset optional.
    check("no filter variable is sent", "filter" in sent["variables"], False)
    check("no filter argument is declared", "filter" in sent["query"], False)
    check(
        "braces are balanced",
        sum((c == "{") - (c == "}") for c in sent["query"]),
        0,
    )

    print("\nbrowser identity travels on the request")
    # Home Assistant overwrites a session's default headers with its own
    # User-Agent after construction, so anything set at session level is
    # silently dropped -- and Origin's WAF answers non-browser clients with
    # HTTP 403. The headers have to be on each request.
    sent_headers = session.headers_sent[0]
    check("sends a User-Agent", "User-Agent" in sent_headers, True)
    check("it is the browser one", sent_headers["User-Agent"], BROWSER_HEADERS["User-Agent"])
    check("does not identify as HomeAssistant", "HomeAssistant" in sent_headers["User-Agent"], False)
    check("still carries the bearer token", sent_headers["Authorization"], "Bearer token")
    for header in ("Origin", "Referer", "sec-ch-ua"):
        check(f"sends {header}", sent_headers.get(header), BROWSER_HEADERS[header])

    print("\nretry policy")
    session = _FakeSession([graphql_error("INTERNAL_SERVER_ERROR"), usage_ok()])
    result = await fetch(session)
    check("retries INTERNAL_SERVER_ERROR", session.calls, 2)
    check("returns data after retry", len(result.points), 1)

    session = _FakeSession([graphql_error("EXTERNAL_API_ERROR")] * 2 + [usage_ok()])
    await fetch(session)
    check("retries EXTERNAL_API_ERROR twice", session.calls, 3)

    session = _FakeSession([graphql_error("USER_INPUT_ERROR")])
    try:
        await fetch(session)
        check("USER_INPUT_ERROR raises", False, True)
    except OriginEnergyApiError:
        check("USER_INPUT_ERROR is not retried", session.calls, 1)

    session = _FakeSession([graphql_error("INTERNAL_SERVER_ERROR")] * 3)
    try:
        await fetch(session)
        check("exhausted retries raise", False, True)
    except OriginEnergyApiError:
        check("gives up after REQUEST_ATTEMPTS", session.calls, 3)

    session = _FakeSession([aiohttp.ClientError("reset"), usage_ok()])
    await fetch(session)
    check("retries transport errors", session.calls, 2)

    session = _FakeSession([_FakeResponse(503, {}), usage_ok()])
    await fetch(session)
    check("retries HTTP 5xx", session.calls, 2)

    session = _FakeSession([_FakeResponse(401, {})])
    try:
        await fetch(session)
        check("HTTP 401 raises", False, True)
    except OriginEnergyApiError:
        check("does not retry HTTP 401", session.calls, 1)

    session = _FakeSession([TimeoutError("slow"), usage_ok()])
    await fetch(session)
    check("retries timeouts", session.calls, 2)

    print("\nenvelope parsing")
    window = await fetch(_FakeSession([usage_ok(points=3)]))
    check("reads the declared energy unit", window.consumption_unit, "WATT_HOUR")
    check("reads the declared cost unit", window.cost_unit, "CENT")
    check("parses lastReadingDate", window.last_reading_date.isoformat(), "2026-07-22T14:00:00+00:00")
    check("carries hasFeedIn", window.has_feed_in, False)
    check("carries hasMissingDataPoints", window.has_missing_points, True)
    check("keeps every data point", len(window.points), 3)

    print(f"\n{'FAILED: %d check(s)' % FAILURES if FAILURES else 'all checks passed'}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    asyncio.run(main())
