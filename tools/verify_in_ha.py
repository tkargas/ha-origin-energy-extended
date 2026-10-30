#!/usr/bin/env python3
"""
Check what the integration actually wrote into Home Assistant.

Reads the recorder database read-only and reports on the statistics the
coordinator produced: how many, over what span, whether the running sums
are monotonic, and whether daily totals match a CSV export from My
Account if one is given.

Deliberately reads nothing but the two statistics tables -- it never opens
.storage, which holds the session cookie.

Usage:
    docker exec -i homeassistant python3 /tmp/origin_energy_test/verify_in_ha.py
    docker exec -i homeassistant python3 /tmp/origin_energy_test/verify_in_ha.py /tmp/origin_energy_test/usage.csv
"""
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB = "/config/home-assistant_v2.db"
TZ = timezone(timedelta(hours=10))  # display only


def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def main() -> int:
    conn = connect()
    metas = conn.execute(
        "SELECT id, statistic_id, unit_of_measurement, has_sum, mean_type, unit_class "
        "FROM statistics_meta WHERE statistic_id LIKE 'origin_energy:%' ORDER BY statistic_id"
    ).fetchall()

    if not metas:
        print("No origin_energy statistics found.")
        print("Add the integration first: Settings -> Devices & services -> Add integration.")
        return 1

    print("statistics metadata")
    for _id, sid, unit, has_sum, mean_type, unit_class in metas:
        print(
            f"  {sid}\n"
            f"      unit={unit!r} has_sum={bool(has_sum)} "
            f"mean_type={mean_type} unit_class={unit_class!r}"
        )

    problems = 0
    daily_kwh = {}

    for meta_id, sid, unit, *_ in metas:
        rows = conn.execute(
            "SELECT start_ts, state, sum FROM statistics WHERE metadata_id = ? "
            "ORDER BY start_ts",
            (meta_id,),
        ).fetchall()
        print(f"\n{sid}")
        if not rows:
            print("  no rows yet -- the backfill may still be running")
            problems += 1
            continue

        first = datetime.fromtimestamp(rows[0][0], timezone.utc).astimezone(TZ)
        last = datetime.fromtimestamp(rows[-1][0], timezone.utc).astimezone(TZ)
        print(f"  {len(rows)} points, {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} (+10:00)")
        print(f"  final running sum: {rows[-1][2]:.3f}")

        # Every point must be on a clock hour, or the Energy dashboard
        # buckets them oddly.
        off_hour = [r for r in rows if int(r[0]) % 3600 != 0]
        print(f"  aligned to the clock hour: {not off_hour}")
        problems += bool(off_hour)

        # Sums are absolute running totals; going backwards means a window
        # was imported out of order.
        drops = [
            (a[0], a[2], b[2])
            for a, b in zip(rows, rows[1:])
            if b[2] is not None and a[2] is not None and b[2] < a[2] - 1e-9
        ]
        print(f"  running sum never decreases: {not drops}")
        if drops:
            problems += 1
            for ts, before, after in drops[:3]:
                when = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
                print(f"      at {when:%Y-%m-%d %H:%M}: {before:.3f} -> {after:.3f}")

        # sum[n] - sum[n-1] should equal state[n].
        mismatches = [
            (b[0], b[1], b[2] - a[2])
            for a, b in zip(rows, rows[1:])
            if b[1] is not None and abs((b[2] - a[2]) - b[1]) > 0.001
        ]
        print(f"  sum increments match state: {not mismatches}")
        if mismatches:
            problems += 1
            for ts, state, delta in mismatches[:3]:
                when = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
                print(f"      at {when:%Y-%m-%d %H:%M}: state {state:.3f} vs delta {delta:.3f}")

        dupes = len(rows) - len({r[0] for r in rows})
        print(f"  no duplicate timestamps: {not dupes}")
        problems += bool(dupes)

        if sid.endswith("_energy_consumption"):
            totals = defaultdict(float)
            for ts, state, _sum in rows:
                if state is None:
                    continue
                day = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ).date()
                totals[day] += state
            daily_kwh = dict(totals)

    if len(sys.argv) > 1 and daily_kwh:
        problems += compare_csv(sys.argv[1], daily_kwh)

    print(f"\n{'PROBLEMS: %d' % problems if problems else 'all checks passed'}")
    return 1 if problems else 0


def compare_csv(path: str, daily_kwh: dict) -> int:
    """Reconcile HA's stored statistics against Origin's own CSV export.

    Origin's export is half-hourly intervals with the columns
    "From (date/time)", "Amount Used" and "Usage Type". Rows are grouped by
    the local date the interval *starts* in, which is the convention its own
    DAILY totals use.
    """
    csv_daily = defaultdict(float)
    skipped = set()
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"From (date/time)", "Amount Used"}
        if not required.issubset(reader.fieldnames or []):
            print(f"\nCSV: expected columns {sorted(required)}, got {reader.fieldnames}")
            return 1
        for row in reader:
            # An account with solar exports both directions into the same
            # file; only consumption is comparable here.
            usage_type = (row.get("Usage Type") or "").strip()
            if usage_type and "consum" not in usage_type.lower():
                skipped.add(usage_type)
                continue
            try:
                start = datetime.fromisoformat(row["From (date/time)"])
                csv_daily[start.date()] += float(row["Amount Used"] or 0)
            except ValueError:
                continue
    if skipped:
        print(f"\nCSV: ignored non-consumption rows: {sorted(skipped)}")

    shared = sorted(set(csv_daily) & set(daily_kwh))
    if not shared:
        print("\nCSV: no overlapping days to compare")
        return 1

    worst = 0.0
    bad = 0
    for day in shared:
        diff = abs(csv_daily[day] - daily_kwh[day])
        worst = max(worst, diff)
        if diff > 0.01:
            bad += 1
            if bad <= 5:
                print(f"  {day}: HA {daily_kwh[day]:.3f} vs CSV {csv_daily[day]:.3f}")

    print(f"\nHA statistics vs Origin CSV export")
    print(f"  {len(shared)} days compared, {bad} outside +/-0.01 kWh (worst {worst:.4f})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
