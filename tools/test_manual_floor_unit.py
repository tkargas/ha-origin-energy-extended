#!/usr/bin/env python3
"""
Offline checks for the "Origin after manual data" behaviour.

Runs without Home Assistant installed: the HA/aiohttp modules are stubbed
and the recorder is replaced by an in-memory statistics table, so the real
coordinator code (_async_backfill, _async_import_recent, _build_statistics)
runs against a fake Origin API and a fake store. When Home Assistant *is*
importable the stubs are skipped for the parts that need no recorder.

Usage:
    python tools/test_manual_floor_unit.py
"""
import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components"))

FAILURES = 0


def check(label, got, want) -> None:
    global FAILURES
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


UTC = timezone.utc


def hr(day, hour, month=10):
    return datetime(2026, month, day, hour, tzinfo=UTC)


# --------------------------------------------------------------------------
# Stubs for everything the coordinator imports
# --------------------------------------------------------------------------


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__["__getattr__"] = lambda attr: MagicMock(name=f"{name}.{attr}")
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class UpdateFailed(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls


class _FakeDt:
    """Just the bits of homeassistant.util.dt the coordinator uses."""

    NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)

    @staticmethod
    def utcnow():
        return _FakeDt.NOW

    @staticmethod
    def now(tz=None):
        return _FakeDt.NOW.astimezone(tz) if tz else _FakeDt.NOW

    @staticmethod
    def utc_from_timestamp(ts):
        return datetime.fromtimestamp(ts, tz=UTC)

    @staticmethod
    def parse_datetime(text):
        return datetime.fromisoformat(text)


def install_stubs():
    for name in ("aiohttp", "yarl"):
        _stub(name, ClientError=type("ClientError", (Exception,), {}), ClientSession=object, URL=lambda *a, **k: MagicMock())
    ha = _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder")
    _stub(
        "homeassistant.components.recorder.models",
        StatisticData=lambda **kw: kw,
        StatisticMetaData=lambda **kw: kw,
    )
    _stub("homeassistant.components.recorder.statistics")
    _stub("homeassistant.config_entries", ConfigEntry=_DataUpdateCoordinator)
    _stub("homeassistant.const")
    _stub("homeassistant.core")
    _stub("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed)
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.aiohttp_client")
    _stub(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    )
    util = _stub("homeassistant.util")
    util.dt = _FakeDt
    _stub("homeassistant.util.dt", **{k: getattr(_FakeDt, k) for k in ("utcnow", "now", "utc_from_timestamp", "parse_datetime")})
    _stub("homeassistant.util.unit_conversion")
    ha.util = util


try:
    import homeassistant  # noqa: F401
    REAL_HA = True
except ImportError:
    REAL_HA = False

# The coordinator needs the stubs even when HA is present (it talks to a
# recorder), so they are installed unconditionally for this test.
for key in [k for k in sys.modules if k == "homeassistant" or k.startswith("homeassistant.")]:
    del sys.modules[key]
install_stubs()

from origin_energy import coordinator as coord  # noqa: E402
from origin_energy.api import UsageWindow  # noqa: E402
from origin_energy.coordinator import OriginEnergyCoordinator, _ImportCursor  # noqa: E402
from origin_energy.manual_floor import (  # noqa: E402
    ManualFloor,
    configured_statistic_id,
    floor_from_last_statistics,
    later_start,
    not_before_floor,
    parse_statistic_id,
)

# --------------------------------------------------------------------------
# 1. The pure helpers
# --------------------------------------------------------------------------
print("manual_floor helpers")
check("blank -> None", parse_statistic_id("   "), None)
check("None -> None", parse_statistic_id(None), None)
check("entity id accepted + lower-cased", parse_statistic_id(" Sensor.Electricity_Meter_Total "), "sensor.electricity_meter_total")
check("external id accepted", parse_statistic_id("origin_energy:123_energy_consumption"), "origin_energy:123_energy_consumption")
for bad in ("sensor", "sensor.", "a.b.c", "has space.x", "sensor.x-y"):
    try:
        parse_statistic_id(bad)
        check(f"malformed {bad!r} rejected", "accepted", "ValueError")
    except ValueError:
        check(f"malformed {bad!r} rejected", "ValueError", "ValueError")
check("stored garbage ignored, not raised", configured_statistic_id({"manual_statistic_id": "nope"}, "manual_statistic_id"), None)
check("stored id read", configured_statistic_id({"manual_statistic_id": "sensor.m"}, "manual_statistic_id"), "sensor.m")

floor = floor_from_last_statistics({"sensor.m": [{"start": hr(30, 9).timestamp(), "sum": 5.0}]}, "sensor.m")
check("floor reads the last row's start", floor, ManualFloor(hr(30, 9)))
check("first free hour is the next one", floor.first_free_start, hr(30, 10))
check("no rows -> no floor", floor_from_last_statistics({"sensor.m": []}, "sensor.m"), None)
check("missing id -> no floor", floor_from_last_statistics({}, "sensor.m"), None)
check("not_before_floor pushes a start forward", not_before_floor(hr(25, 0), floor), hr(30, 10))
check("not_before_floor leaves a later start", not_before_floor(hr(31, 0), floor), hr(31, 0))
check("not_before_floor without a floor", not_before_floor(hr(25, 0), None), hr(25, 0))
check("later_start: None + floor", later_start(None, floor), hr(30, 9))
check("later_start: cursor already later", later_start(hr(31, 0), floor), hr(31, 0))
check("later_start: floor later", later_start(hr(25, 0), floor), hr(30, 9))
check("later_start: no floor keeps cursor", later_start(hr(25, 0), None), hr(25, 0))

# --------------------------------------------------------------------------
# 2. The coordinator against a fake store and a fake Origin
# --------------------------------------------------------------------------
OWN = "origin_energy:111_energy_consumption"
OWN_COST = "origin_energy:111_energy_cost"
MANUAL = "sensor.electricity_meter_total"


class Store:
    """In-memory stand-in for the recorder's hourly statistics."""

    def __init__(self):
        self.rows = {}  # statistic_id -> {start: {"state":..,"sum":..}}
        self.writes = []  # (statistic_id, start) in order

    def put(self, statistic_id, start, state, total):
        self.rows.setdefault(statistic_id, {})[start] = {"state": state, "sum": total}

    def snapshot(self, statistic_id):
        return {s: dict(r) for s, r in self.rows.get(statistic_id, {}).items()}

    # -- the functions the coordinator calls
    def get_last_statistics(self, hass, n, statistic_id, convert, types_):
        rows = self.rows.get(statistic_id)
        if not rows:
            return {}
        start = max(rows)
        return {statistic_id: [{"start": start.timestamp(), "sum": rows[start]["sum"]}]}

    def statistics_during_period(self, hass, start, end, ids, period, units, types_):
        out = {}
        for sid in ids:
            got = [
                {"start": s.timestamp(), "sum": r["sum"]}
                for s, r in sorted(self.rows.get(sid, {}).items())
                if start <= s < end
            ]
            if got:
                out[sid] = got
        return out

    def clear(self, statistic_ids):
        self.cleared = list(statistic_ids)
        for sid in statistic_ids:
            self.rows.pop(sid, None)

    def add_external(self, hass, metadata, stats):
        for row in stats:
            self.put(metadata["statistic_id"], row["start"], row["state"], row["sum"])
            self.writes.append((metadata["statistic_id"], row["start"]))


class FakeOrigin:
    """Hourly data, 1 kWh-ish per hour, published up to last_reading."""

    def __init__(self, first, last_published_hour):
        self.first = first
        self.last = last_published_hour  # last hour that has data
        self.queries = []

    @staticmethod
    def wh(hour):
        return (int(hour.timestamp() // 3600) % 7 + 1) * 100  # 100..700 Wh

    async def async_get_usage(self, agreement, start, end, unit):
        self.queries.append((start, end))
        points = []
        h = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        while h <= end and len(points) < 24 * 8:
            has = self.first <= h <= self.last
            points.append(
                {
                    "type": "ACTUAL" if has else "MISSING",
                    "startDate": h.isoformat(timespec="milliseconds"),
                    "totalConsumedEnergy": self.wh(h) if has else None,
                    "totalCost": 30 if has else None,
                }
            )
            h += timedelta(hours=1)
        return UsageWindow(
            points=points,
            last_reading_date=self.last + timedelta(hours=1),
            consumption_unit="WATT_HOUR",
            cost_unit="CENT",
            has_feed_in=False,
            has_missing_points=False,
        )


def make(store, origin, manual_id=None, backfill_start=None):
    store_ns = types.SimpleNamespace(
        get_instance=lambda hass: types.SimpleNamespace(
            async_add_executor_job=_run_job,
            async_clear_statistics=store.clear,
        ),
        get_last_statistics=store.get_last_statistics,
        statistics_during_period=store.statistics_during_period,
        async_add_external_statistics=store.add_external,
    )
    for name, value in vars(store_ns).items():
        setattr(coord, name, value)

    c = object.__new__(OriginEnergyCoordinator)
    c.hass = MagicMock()
    c.config_entry = MagicMock()
    c.config_entry.data = {}
    c.config_entry.options = {"manual_statistic_id": manual_id} if manual_id else {}
    c.agreement_id = "111"
    c.timezone = ZoneInfo("UTC")
    c.time_unit = "HOURLY"
    c.consumption_statistic_id = OWN
    c.cost_statistic_id = OWN_COST
    c._manual_statistic_id = manual_id
    c._backfill_task = None
    c._import_lock = asyncio.Lock()
    c.backfill_started = 0
    c.async_request_refresh = lambda: None

    async def _api():
        return origin

    async def _history_start():
        return backfill_start or origin.first

    c._async_api = _api
    c._async_history_start = _history_start
    c._start_backfill = lambda: setattr(c, "backfill_started", c.backfill_started + 1)
    return c


async def _run_job(fn, *args):
    return fn(*args)


def put_manual(store, first, last, base=1000.0):
    """Hand-entered hourly rows with a running sum, 0.5 kWh per hour."""
    h, total = first, base
    while h <= last:
        total += 0.5
        store.put(MANUAL, h, 0.5, total)
        h += timedelta(hours=1)


def hours(first, last):
    out, h = [], first
    while h <= last:
        out.append(h)
        h += timedelta(hours=1)
    return out


def run(coro):
    return asyncio.run(coro)


# ---- A: manual data already covers everything Origin has --------------------
print("A. manual covers all of Origin's data")
store = Store()
put_manual(store, hr(20, 0), hr(30, 9))
before = store.snapshot(MANUAL)
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, MANUAL)
run(c._async_backfill())
check("nothing written to Origin's series", store.snapshot(OWN), {})
check("manual rows untouched", store.snapshot(MANUAL), before)
check("no write ever targeted the manual statistic", [w for w in store.writes if w[0] == MANUAL], [])
check("backfill still marks complete", c.config_entry.__class__ is not None and c.hass.config_entries.async_update_entry.called, True)

# ---- B: Origin is ahead of the manual data: only later hours are added ------
print("B. Origin ahead of manual: backfill adds only later hours")
store = Store()
put_manual(store, hr(20, 0), hr(28, 23))
before = store.snapshot(MANUAL)
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, MANUAL)
run(c._async_backfill())
own = store.snapshot(OWN)
want_hours = hours(hr(29, 0), hr(29, 23))
check("only the hours after the last manual hour", sorted(own), want_hours)
check("manual rows untouched", store.snapshot(MANUAL), before)
total = 0.0
sums_ok = True
for h in want_hours:
    total += FakeOrigin.wh(h) / 1000
    sums_ok &= abs(own[h]["sum"] - total) < 1e-9
check("Origin's running sum accumulates from the first added hour", sums_ok, True)
check("API was asked only from the first free hour", min(q[0] for q in origin.queries) >= hr(29, 0), True)

# ---- C: next poll after Origin publishes another day -----------------------
print("C. next poll appends further hours, never rewrites manual or earlier rows")
origin.last = hr(30, 5)  # Origin publishes into today
store.writes.clear()
run(c._async_import_recent())
own2 = store.snapshot(OWN)
check("new hours appended", sorted(own2), hours(hr(29, 0), hr(30, 5)))
check("manual still untouched", store.snapshot(MANUAL), before)
check("no write targeted the manual statistic", [w for w in store.writes if w[0] == MANUAL], [])
check("no row at or before the manual floor was written", [w for w in store.writes if w[1] <= hr(28, 23)], [])
total = 0.0
ok = True
for h in sorted(own2):
    total += FakeOrigin.wh(h) / 1000
    ok &= abs(own2[h]["sum"] - total) < 1e-9
check("sums stay continuous across the join", ok, True)

# ---- D: Origin's series already has old rows covering manual hours ----------
print("D. option added after Origin's series already existed")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c0 = make(store, origin, None)
run(c0._async_backfill())  # old behaviour: everything
old = store.snapshot(OWN)
check("setup: old series covers every hour", sorted(old), hours(hr(20, 0), hr(29, 23)))
put_manual(store, hr(20, 0), hr(27, 23))
manual_before = store.snapshot(MANUAL)
# Origin revises the last days: change every value
origin.wh = staticmethod(lambda h: (int(h.timestamp() // 3600) % 5 + 1) * 100)
store.writes.clear()
c = make(store, origin, MANUAL)
run(c._async_import_recent())
new = store.snapshot(OWN)
check("rows at or before the manual floor are exactly as they were",
      {h: r for h, r in new.items() if h <= hr(27, 23)}, {h: r for h, r in old.items() if h <= hr(27, 23)})
check("nothing written at or before the floor", [w for w in store.writes if w[1] <= hr(27, 23)], [])
check("hours after the floor were refreshed", any(w[1] > hr(27, 23) for w in store.writes), True)
base_sum = old[hr(27, 23)]["sum"]
total = base_sum
ok = True
for h in hours(hr(28, 0), hr(29, 23)):
    if h in new and h >= hr(28, 0):
        total += origin.wh(h) / 1000
        ok &= abs(new[h]["sum"] - total) < 1e-9
check("sums continue from the series' own sum at the floor", ok, True)
check("manual untouched", store.snapshot(MANUAL), manual_before)

# ---- E: configured but the statistic has no data -> refuse -------------------
print("E. manual statistic id with no data")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, "sensor.typo")
try:
    run(c._async_import_recent())
    check("raises UpdateFailed", "no error", "UpdateFailed")
except UpdateFailed as err:
    check("raises UpdateFailed naming the id", "sensor.typo" in str(err), True)
check("nothing was written", store.writes, [])

# ---- F: no option: behaviour as before (overlap rewrite) ----------------------
print("F. without the option the overlap still rewrites trailing days")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c = make(store, origin, None)
run(c._async_backfill())
store.writes.clear()
run(c._async_import_recent())
check("revision overlap rewrote earlier hours", min(w[1] for w in store.writes if w[0] == OWN) < hr(29, 0), True)

# ---- G: floor ahead of Origin's data: early out, no baseline lookups ----------
print("G. recent update when Origin has not reached the manual data")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c0 = make(store, origin, None)
run(c0._async_backfill())
put_manual(store, hr(20, 0), hr(30, 9))
store.writes.clear()
c = make(store, origin, MANUAL)
run(c._async_import_recent())
check("nothing written", store.writes, [])

# ---- H: option change is noticed ------------------------------------------------
print("I. clear Origin's overlapping history, then re-import after the manual data")
store = Store()
origin = FakeOrigin(hr(20, 0), hr(29, 23))
c0 = make(store, origin, None)
run(c0._async_backfill())
put_manual(store, hr(20, 0), hr(28, 23))
manual_before = store.snapshot(MANUAL)
check("setup: Origin's series double covers the manual days", hr(25, 0) in store.snapshot(OWN), True)
c = make(store, origin, None)
c.config_entry.data = {"backfill_complete": True}
run(c.async_reset_for_manual_statistic(MANUAL))
check("only Origin's own two statistics were cleared", sorted(store.cleared), sorted([OWN, OWN_COST]))
check("Origin's series is empty", store.snapshot(OWN), {})
check("manual untouched", store.snapshot(MANUAL), manual_before)
check("backfill flag turned off", c.hass.config_entries.async_update_entry.call_args.kwargs["data"]["backfill_complete"], False)
run(c._async_backfill())
own = store.snapshot(OWN)
check("re-import adds only hours after the manual data", sorted(own), hours(hr(29, 0), hr(29, 23)))
check("manual still untouched", store.snapshot(MANUAL), manual_before)
try:
    run(make(Store(), origin, None).async_reset_for_manual_statistic(OWN))
    check("manual id equal to Origin's own id is refused", "accepted", "ValueError")
except ValueError:
    check("manual id equal to Origin's own id is refused", "ValueError", "ValueError")

print("H. adopting a changed option")
c = make(Store(), FakeOrigin(hr(20, 0), hr(29, 23)), None)
check("no change -> False", c.async_adopt_manual_statistic(), False)
c.config_entry.options = {"manual_statistic_id": "sensor.electricity_meter_total"}
check("set -> True", c.async_adopt_manual_statistic(), True)
check("then stable -> False", c.async_adopt_manual_statistic(), False)
c.config_entry.options = {}
check("cleared -> True", c.async_adopt_manual_statistic(), True)

print()
if FAILURES:
    print(f"{FAILURES} check(s) FAILED")
    sys.exit(1)
print("all checks passed")
