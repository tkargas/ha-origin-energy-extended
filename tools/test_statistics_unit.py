#!/usr/bin/env python3
"""
Offline checks for the statistics the coordinator writes.

This is the code whose mistakes are expensive: a wrong running sum or a
double-counted boundary point lands in Home Assistant's long-term
statistics, where it shows up as a spike on the Energy dashboard and has
to be deleted by hand. _build_statistics touches no instance state, so it
can be exercised directly without standing up Home Assistant.

Usage:
    docker exec -i homeassistant python3 /tmp/origin_energy_test/test_statistics_unit.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from origin_energy.api import UsageWindow
from origin_energy.coordinator import (
    OriginEnergyCoordinator,
    _ImportCursor,
    statistic_slug,
)

FAILURES = 0


def check(label, got, want) -> None:
    global FAILURES
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


def point(hour, energy, cost=None, offset="+10:00", day=22):
    """One data point; energy=None models a MISSING bucket."""
    return {
        "type": "ACTUAL" if energy is not None else "MISSING",
        "startDate": f"2026-07-{day:02d}T{hour:02d}:00:00.000{offset}",
        "totalConsumedEnergy": energy,
        "totalCost": cost,
    }


def window(points, consumption_unit="WATT_HOUR", cost_unit="CENT"):
    return UsageWindow(
        points=points,
        last_reading_date=None,
        consumption_unit=consumption_unit,
        cost_unit=cost_unit,
        has_feed_in=False,
        has_missing_points=any(p["totalConsumedEnergy"] is None for p in points),
    )


def build(result, cursor):
    # Unbound: the method reads nothing off self.
    return OriginEnergyCoordinator._build_statistics(None, result, cursor)


def main() -> None:
    print("running sums")
    cursor = _ImportCursor()
    consumption, cost = build(window([point(1, 1000), point(2, 2000), point(3, 500)]), cursor)
    check("converts Wh to kWh", [c["state"] for c in consumption], [1.0, 2.0, 0.5])
    check("accumulates the sum", [c["sum"] for c in consumption], [1.0, 3.0, 3.5])
    check("cursor carries the total", cursor.consumption_sum, 3.5)
    check("cursor tracks the last point", cursor.last_start.hour, 3)

    print("\nsums continue across windows")
    consumption, _ = build(window([point(4, 1500)]), cursor)
    check("second window resumes", [c["sum"] for c in consumption], [5.0])

    print("\nMISSING buckets")
    cursor = _ImportCursor()
    consumption, _ = build(window([point(1, 1000), point(2, None), point(3, 500)]), cursor)
    check("skips null consumption", [c["state"] for c in consumption], [1.0, 0.5])
    check("sum ignores the gap", [c["sum"] for c in consumption], [1.0, 1.5])

    print("\nboundary de-duplication")
    # Origin's ranges are inclusive at both ends, so consecutive windows
    # repeat the shared point; counting it twice would inflate the sum.
    cursor = _ImportCursor(last_start=dt_util.parse_datetime("2026-07-22T02:00:00.000+10:00"))
    consumption, _ = build(window([point(1, 1000), point(2, 2000), point(3, 500)]), cursor)
    check("drops already-imported points", [c["state"] for c in consumption], [0.5])
    check("sum counts the new point only", cursor.consumption_sum, 0.5)

    print("\ncost conversion")
    cursor = _ImportCursor()
    _, cost = build(window([point(1, 493, 22.143)]), cursor)
    check("converts cents to dollars", round(cost[0]["state"], 5), 0.22143)
    cursor = _ImportCursor()
    _, cost = build(window([point(1, 493)]), cursor)
    check("treats a null cost as zero", cost[0]["state"], 0.0)

    print("\ndeclared units are honoured")
    cursor = _ImportCursor()
    consumption, cost = build(
        window([point(1, 2.5, 1.25)], consumption_unit="KILOWATT_HOUR", cost_unit="DOLLAR"),
        cursor,
    )
    check("kWh passes through unscaled", consumption[0]["state"], 2.5)
    check("dollars pass through unscaled", cost[0]["state"], 1.25)

    cursor = _ImportCursor()
    consumption, _ = build(window([point(1, 1)], consumption_unit="MEGAWATT_HOUR"), cursor)
    check("scales MWh", consumption[0]["state"], 1000.0)

    print("\nunknown units refuse to guess")
    try:
        build(window([point(1, 1000)], consumption_unit="FURLONGS"), _ImportCursor())
        check("unknown energy unit raises", False, True)
    except UpdateFailed as err:
        check("unknown energy unit raises", "FURLONGS" in str(err), True)
    try:
        build(window([point(1, 1000)], cost_unit="DOUBLOON"), _ImportCursor())
        check("unknown cost unit raises", False, True)
    except UpdateFailed as err:
        check("unknown cost unit raises", "DOUBLOON" in str(err), True)

    print("\nend of daylight saving")
    # Sydney repeats 02:00 on the first Sunday in April: once at +11:00,
    # then an hour later at +10:00. Sorted as text the later point comes
    # first, which would make the running sums go backwards -- so the
    # parsed instants have to drive the order.
    cursor = _ImportCursor()
    folded = [
        point(2, 1000, offset="+10:00", day=5),  # chronologically second
        point(2, 2000, offset="+11:00", day=5),  # chronologically first
        point(3, 3000, offset="+10:00", day=5),
    ]
    check(
        "the API's string order is the wrong order",
        [p["startDate"] for p in folded] == sorted(p["startDate"] for p in folded),
        True,
    )
    consumption, _ = build(window(folded), cursor)
    check("re-sorts the repeated hour", [c["state"] for c in consumption], [2.0, 1.0, 3.0])
    check("sums stay monotonic", [c["sum"] for c in consumption], [2.0, 3.0, 6.0])
    check("no point is dropped", len(consumption), 3)

    print("\nstatistic ids Home Assistant will accept")
    check("plain digits pass through", statistic_slug("40318827"), "40318827")
    check("hyphens become underscores", statistic_slug("A-1B2C-3D4"), "a_1b2c_3d4")
    check("runs collapse, edges trimmed", statistic_slug("--A??B--"), "a_b")
    check("an unusable id still yields something", statistic_slug("!!!"), "unknown")
    for raw in ("40318827", "A-1B2C3D4E-4031", "acct/99", "  x  "):
        stat_id = f"origin_energy:{statistic_slug(raw)}_energy_consumption"
        check(f"valid_statistic_id({stat_id})", valid_statistic_id(stat_id), True)

    print("\nlocal offsets are preserved")
    cursor = _ImportCursor()
    consumption, _ = build(window([point(23, 493)]), cursor)
    check("keeps Origin's offset", consumption[0]["start"].utcoffset().total_seconds(), 36000.0)
    check("stays on the clock hour", consumption[0]["start"].minute, 0)

    print(f"\n{'FAILED: %d check(s)' % FAILURES if FAILURES else 'all checks passed'}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
