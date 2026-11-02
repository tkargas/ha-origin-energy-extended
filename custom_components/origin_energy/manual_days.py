"""Which days already have manually-entered data, so Origin's can skip them.

Some people keep a statistic of hand-entered readings for the same meter
(an entity such as ``sensor.electricity_meter_total``). For any local day on
which that statistic recorded consumption, Origin's data for the same day
must not be added alongside it, or the Energy dashboard counts the day
twice. Every other day -- before, after, or in a gap between manual days --
is added as normal. Origin's data for a manual day may be fetched, but it is
never written, and the manual statistic itself is only ever read.

"Has manual data" means a non-zero change in the manual statistic during that
local day. A statistic that is still being recorded (a live sensor adds a row
every hour) has rows for every hour, but on days nobody entered anything
those rows carry no change, so they do not count.

Kept free of Home Assistant imports so it can be tested on its own
(tools/test_manual_days_unit.py).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

# An entity ("sensor.electricity_meter_total") or an external statistic
# ("origin_energy:123_energy_consumption"): lowercase letters, digits and
# underscores either side of a single '.' or ':'.
_STATISTIC_ID_RE = re.compile(r"^[a-z0-9_]+[.:][a-z0-9_]+$")

# Changes smaller than this are rounding noise, not an entry.
MIN_CHANGE = 1e-6


def parse_statistic_id(value: Any) -> str | None:
    """Tidy a user-entered statistic id. None for blank, ValueError if malformed."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if not _STATISTIC_ID_RE.match(text):
        raise ValueError(f"Not a valid statistic id: {value!r}")
    return text


def configured_statistic_id(options: Mapping[str, Any], key: str) -> str | None:
    """The manual statistic id from stored options, ignoring anything unusable.

    Options survive hand-edited .storage files and downgrades, so a bad
    value means "no manual statistic" rather than an exception on every poll.
    """
    try:
        return parse_statistic_id(options.get(key))
    except ValueError:
        return None


def local_day(moment: datetime, tz: tzinfo) -> date:
    """The calendar day `moment` falls on in `tz`."""
    return moment.astimezone(tz).date()


def days_with_manual_data(
    rows: Iterable[Mapping[str, Any]] | None, tz: tzinfo
) -> frozenset[date]:
    """Local days on which the manual statistic changed.

    `rows` is ``statistics_during_period(..., "hour", None, {"change"})``
    output for the manual statistic: dicts with a ``start`` timestamp and a
    ``change``.
    """
    days: set[date] = set()
    for row in rows or ():
        change = row.get("change")
        if change is None or abs(float(change)) < MIN_CHANGE:
            continue
        start = datetime.fromtimestamp(float(row["start"]), tz=timezone.utc)
        days.add(local_day(start, tz))
    return frozenset(days)


def last_manual_entry(
    rows: Iterable[Mapping[str, Any]] | None, tz: tzinfo
) -> tuple[date, float] | None:
    """The last day the manual statistic changed, and its reading now.

    `rows` is ``statistics_during_period(..., "hour", None, {"change", "state"})``
    output, oldest first. The reading is the statistic's latest ``state``: once
    nobody is entering anything the sensor just holds its last value, so that
    is the last reading entered. None when it never changed.
    """
    ordered = list(rows or ())
    last_day: date | None = None
    for row in ordered:
        change = row.get("change")
        if change is None or abs(float(change)) < MIN_CHANGE:
            continue
        start = datetime.fromtimestamp(float(row["start"]), tz=timezone.utc)
        last_day = local_day(start, tz)
    if last_day is None:
        return None
    for row in reversed(ordered):
        if row.get("state") is not None:
            return last_day, float(row["state"])
    return None


def end_of_local_day(day: date, tz: tzinfo) -> datetime:
    """The instant `day` ends in `tz` (the next local midnight), as UTC."""
    return datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(
        timezone.utc
    )


def expand_to_local_days(
    start: datetime, end: datetime, tz: tzinfo
) -> tuple[datetime, datetime]:
    """Widen [start, end] to whole local days, as UTC instants.

    A manual entry anywhere in a day covers all of it, so the manual
    statistic has to be read for every day the import touches, not just the
    hours inside the import's own range.
    """
    first = datetime.combine(start.astimezone(tz).date(), time(0), tzinfo=tz)
    last = datetime.combine(
        end.astimezone(tz).date() + timedelta(days=1), time(0), tzinfo=tz
    )
    return first.astimezone(timezone.utc), last.astimezone(timezone.utc)
