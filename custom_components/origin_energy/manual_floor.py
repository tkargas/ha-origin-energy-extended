"""Where manually-entered statistics end, so Origin's data can start after them.

Some people already have a statistic holding hand-entered readings for the
same meter (an entity such as ``sensor.electricity_meter_total``). Origin's
hourly points for those hours must not be added alongside them, or the
Energy dashboard counts the same hours twice. The rule is "manual wins":
Origin's hours at or before the last row of that statistic are skipped, and
only later hours are written -- into Origin's own statistic, never into the
manual one.

Kept free of Home Assistant imports so it can be tested on its own
(tools/test_manual_floor_unit.py).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# An entity ("sensor.electricity_meter_total") or an external statistic
# ("origin_energy:123_energy_consumption"): lowercase letters, digits and
# underscores either side of a single '.' or ':'.
_STATISTIC_ID_RE = re.compile(r"^[a-z0-9_]+[.:][a-z0-9_]+$")

# Statistics are hourly buckets keyed by their start.
STATISTIC_PERIOD = timedelta(hours=1)


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


@dataclass(frozen=True)
class ManualFloor:
    """The last hour the manual statistic already covers."""

    last_start: datetime  # start of that hour, timezone-aware (UTC)

    @property
    def first_free_start(self) -> datetime:
        """Start of the first hour Origin is allowed to write."""
        return self.last_start + STATISTIC_PERIOD


def floor_from_last_statistics(
    rows: Mapping[str, list[Mapping[str, Any]]] | None, statistic_id: str
) -> ManualFloor | None:
    """Build the floor from ``get_last_statistics(hass, 1, id, ...)`` output."""
    entries = (rows or {}).get(statistic_id) or []
    if not entries:
        return None
    return ManualFloor(
        last_start=datetime.fromtimestamp(float(entries[0]["start"]), tz=timezone.utc)
    )


def not_before_floor(start: datetime, floor: ManualFloor | None) -> datetime:
    """`start`, pushed forward to the first hour Origin may write."""
    if floor is None:
        return start
    return max(start, floor.first_free_start)


def later_start(current: datetime | None, floor: ManualFloor | None) -> datetime | None:
    """Combine an import cursor's last_start with the floor: the later wins."""
    if floor is None:
        return current
    if current is None:
        return floor.last_start
    return max(current, floor.last_start)
