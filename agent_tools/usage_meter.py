"""Reads the claude-code status-line rate-limit JSON into a `Meter` and turns an entry into a `pacing.Window`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import pacing, run_store

__all__ = [
    "DEFAULT_PATH",
    "IMPLIED_PATH",
    "STATE_DIR",
    "Meter",
    "MeterEntry",
    "as_window",
    "fresh",
    "implied_ceiling",
    "parse",
    "prefer",
    "read",
    "record_implied_ceiling",
    "usable_percentage",
]

_SPANS = {"five_hour": timedelta(hours=5), "weekly": timedelta(days=7)}
_MIN_ELAPSED = {"five_hour": timedelta(hours=1), "weekly": timedelta(days=1)}
_FRESH_AGE = timedelta(minutes=15)

STATE_DIR = Path.home() / ".local" / "state" / "coxswain"
DEFAULT_PATH = STATE_DIR / "rate-limits.json"
IMPLIED_PATH = STATE_DIR / "implied-ceilings.json"

_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class MeterEntry:
    used_percentage: float
    resets_at: datetime


@dataclass(frozen=True)
class Meter:
    five_hour: MeterEntry
    seven_day: MeterEntry
    observed_at: datetime


def _entry(doc: dict) -> MeterEntry:
    return MeterEntry(
        used_percentage=float(doc["used_percentage"]),
        resets_at=datetime.fromtimestamp(doc["resets_at"], tz=UTC),
    )


def parse(doc: dict) -> Meter | None:
    try:
        return Meter(
            five_hour=_entry(doc["five_hour"]),
            seven_day=_entry(doc["seven_day"]),
            observed_at=datetime.fromisoformat(doc["observed_at"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


def fresh(meter: Meter, now: datetime, max_age: timedelta = timedelta(minutes=15)) -> bool:
    return now - meter.observed_at <= max_age


def _burn(used: float, elapsed_hours: float) -> float:
    """No elapsed time, e.g. clock skew past the window start, never divides: spend then is infinite pace, as in `pacing._ratio`."""
    if elapsed_hours > 0:
        return used / elapsed_hours
    if used > 0:
        return float("inf")
    return 0.0


def as_window(entry: MeterEntry, now: datetime, span: timedelta) -> pacing.Window:
    """`end` is the reset, not `now`: an `end` of `now` reads as a spent block, a zero countdown and full elapsed pace."""
    start = entry.resets_at - span
    return pacing.Window(
        start=start,
        end=entry.resets_at,
        spent_usd=entry.used_percentage,
        ceiling_usd=100.0,
        burn_usd_per_hour=_burn(entry.used_percentage, (now - start) / _HOUR),
        runs_in_flight=0,
    )


def read(path: Path = DEFAULT_PATH) -> Meter | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None
    return parse(doc) if isinstance(doc, dict) else None


def prefer(meter_window: pacing.Window | None, estimate_window: pacing.Window) -> pacing.Window:
    return meter_window if meter_window is not None else estimate_window


def _load_json_object(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return {}
    return doc if isinstance(doc, dict) else {}


def record_implied_ceiling(
    kind: str,
    meter_entry: MeterEntry,
    runs_dir: Path,
    now: datetime,
    path: Path = IMPLIED_PATH,
) -> None:
    """Calibrates over the meter entry's own window, not `usage_window.gather`'s floating five-hour block.
    Writes nothing below the 5% floor, nor before an hour (five_hour) or a day (weekly) of that window has elapsed."""
    if meter_entry.used_percentage < 5:
        return
    span = _SPANS[kind]
    window_start = meter_entry.resets_at - span
    if now - window_start < _MIN_ELAPSED[kind]:
        return
    dollars = run_store.cost_since(runs_dir, window_start.isoformat())
    if dollars is None:
        return
    implied_usd = dollars / (meter_entry.used_percentage / 100)
    doc = _load_json_object(path)
    doc[kind] = {"ceiling_usd": implied_usd, "observed_at": now.isoformat()}
    path.write_text(json.dumps(doc))


def implied_ceiling(
    kind: str,
    now: datetime,
    path: Path = IMPLIED_PATH,
    max_age: timedelta = timedelta(days=7),
) -> float | None:
    doc = _load_json_object(path)
    try:
        entry = doc[kind]
        ceiling_usd = float(entry["ceiling_usd"])
        observed_at = datetime.fromisoformat(entry["observed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if now - observed_at <= max_age:
        return ceiling_usd
    return None


def usable_percentage(
    kind: str,
    entry: MeterEntry,
    observed_at: datetime,
    now: datetime,
    runs_dir: Path,
    ceiling_usd: float | None,
    path: Path = IMPLIED_PATH,
) -> float | None:
    """A second path alongside `fresh`: a reading within 15 minutes reads at face value, one past its
    `resets_at` is never usable, and one in between is carried forward by the dollars spent since
    `observed_at` over the implied (or caller-supplied) ceiling, same as `fresh` used to drop outright."""
    if now - observed_at <= _FRESH_AGE:
        return entry.used_percentage
    if now >= entry.resets_at:
        return None
    ceiling = implied_ceiling(kind, now, path) or ceiling_usd
    spent_since = run_store.cost_since(runs_dir, observed_at.isoformat())
    if spent_since is None or not ceiling:
        return None
    return entry.used_percentage + (spent_since / ceiling) * 100
