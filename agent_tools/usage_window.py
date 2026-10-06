"""Edge for agent_tools/pacing.py: gathers the current spend window and hands
it to the pure planner. Every impure part lives here — invoking ccusage,
reading usage files, and the clock — so pacing.py never touches any of it.

Preferred source is `ccusage blocks --active --json`. When ccusage is
unavailable, answers empty, or has no block marked active, the fallback is
the run records' own `*.usage.json` files: a window of `window_hours` ending
at `now`, summing only the files whose run started inside it. A caller with
no `ceiling_usd` to pass leaves it `None`, and `pacing.assess` reports the
unmeasured case: pace and projection, no headroom or verdict past `go`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import zoneinfo
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from agent_tools import run_store
from agent_tools.pacing import Policy, Window

__all__ = [
    "CCUSAGE_CACHE_S", "DEFAULT_POLICY", "WeeklyReset", "block_remaining", "ceiling_remaining",
    "gather", "gather_weekly", "parse_weekly_reset", "read_usage", "usage_cost_usd",
    "weekly_window_from", "weekly_window_start", "window_from",
]

# Every pacing check shells out to ccusage (a ~2.5 s Node process) and they run
# seconds apart, so a parsed answer is reused for this long. Only a successful
# parse is cached; a failed run is retried on the next call.
CCUSAGE_CACHE_S = 60
_CCUSAGE_CACHE_FILE = ".ccusage-block.json"

# Used wherever the resolved cartridge dict carries no `policy.pacing` key
# yet (the cross-repository policy has not landed). These are the real
# ladders `cartridges-pacing-policy` declares, not placeholders: an absent
# policy must read as unmeasured-and-uncapped, i.e. tier_ceiling="deep" and
# effort_ceiling="high", never as a tighten nobody asked for.
DEFAULT_POLICY = Policy(
    pace_thresholds=(1.2, 1.5, 2.0, 2.5),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=0.0,
    hard_stop_fraction=0.99,
    weekly_hard_stop_fraction=0.93,
)


class WeeklyReset(NamedTuple):
    """A configured weekly spend-window reset, parsed from `spend.weekly_reset`.
    `weekday` follows `datetime.weekday()`: Monday=0 .. Sunday=6. `tz` is an
    IANA zone name such as `America/New_York`."""
    weekday: int
    hour: int
    minute: int
    tz: str


_WEEKDAY_ABBR = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def parse_weekly_reset(value: str | None) -> WeeklyReset | None:
    """Pure parser for `spend.weekly_reset`, format `<Dow> <HH:MM> <Zone>` such
    as `Sun 04:00 America/New_York`. `None` or anything unparseable returns
    `None` and never raises: absent config is how a profile-less machine
    keeps the rolling window."""
    if value is None:
        return None
    parts = value.split()
    if len(parts) != 3:
        return None
    dow, clock, zone = parts
    weekday = _WEEKDAY_ABBR.get(dow)
    if weekday is None:
        return None
    hour_text, sep, minute_text = clock.partition(":")
    if not sep or not hour_text or not minute_text:
        return None
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    try:
        zoneinfo.ZoneInfo(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None
    return WeeklyReset(weekday=weekday, hour=hour, minute=minute, tz=zone)


def _active_block(blocks_json: dict[str, Any]) -> dict[str, Any] | None:
    """The one block ccusage marked active and that parses cleanly, or None.
    A block that fails to parse is ignored, not raised."""
    for block in blocks_json.get("blocks") or []:
        if not isinstance(block, dict) or not block.get("isActive"):
            continue
        try:
            start = datetime.fromisoformat(str(block["startTime"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(block["endTime"]).replace("Z", "+00:00"))
            spent = float(block["costUSD"])
        except (KeyError, TypeError, ValueError):
            continue
        return {"start": start, "end": end, "spent": spent}
    return None


def window_from(
    blocks_json: dict[str, Any],
    usage_files: list[tuple[datetime, dict[str, Any]]],
    now: datetime,
    window_hours: float = 5.0,
    ceiling_usd: float | None = None,
) -> Window:
    """Pure. `usage_files` is `(start, parsed usage.json)` pairs; the caller
    supplies each start, this function never stats a file."""
    active = _active_block(blocks_json)
    if active is not None:
        span_hours = max((active["end"] - active["start"]).total_seconds() / 3600, 1e-9)
        return Window(
            start=active["start"], end=active["end"],
            spent_usd=active["spent"], ceiling_usd=ceiling_usd,
            burn_usd_per_hour=active["spent"] / span_hours, runs_in_flight=0,
        )

    start = now - timedelta(hours=window_hours)
    in_window = [usage for ts, usage in usage_files if start <= ts <= now]
    spent_usd = sum(usage_cost_usd(u) for u in in_window)
    elapsed_hours = max((now - start).total_seconds() / 3600, 1e-9)
    return Window(
        start=start, end=now,
        spent_usd=spent_usd, ceiling_usd=ceiling_usd,
        burn_usd_per_hour=spent_usd / elapsed_hours, runs_in_flight=len(in_window),
    )


def block_remaining(window: Window, now: datetime) -> tuple[int, float]:
    """Whole minutes left in the block and the fraction still to run, clamped to [0, 1]."""
    span = (window.end - window.start).total_seconds()
    left = max((window.end - now).total_seconds(), 0.0)
    return int(left // 60), max(0.0, min(1.0, left / span)) if span > 0 else 0.0


def ceiling_remaining(window: Window) -> float | None:
    """Fraction of `ceiling_usd` unspent, clamped to [0, 1]; None when there is no ceiling."""
    if window.ceiling_usd is None or window.ceiling_usd <= 0:
        return None
    return max(0.0, min(1.0, (window.ceiling_usd - window.spent_usd) / window.ceiling_usd))


def usage_cost_usd(usage: Mapping[str, Any]) -> float:
    """A run's cost: `summary.cost_usd` in the current file shape, else a
    top-level `cost_usd` for older files, else `0.0`."""
    summary = usage.get("summary") or {}
    return float(summary.get("cost_usd") or usage.get("cost_usd") or 0.0)


def _usage_started(usage: Mapping[str, Any], mtime: datetime) -> datetime:
    """A run's start time: `summary.started_at` when the file carries one, else `mtime`."""
    started_at = (usage.get("summary") or {}).get("started_at")
    try:
        return datetime.fromisoformat(str(started_at)) if started_at else mtime
    except ValueError:
        return mtime


def _store_started(started: Mapping[str, str], run_id: str) -> datetime | None:
    """The run's `launched_at` from `started` (run_store.runs_started's map) as a datetime; None when absent or unparseable."""
    launched_at = started.get(run_id)
    try:
        return datetime.fromisoformat(launched_at) if launched_at else None
    except ValueError:
        return None


def _read_usage_files(
    runs_dir: Path | str, now: datetime, since: datetime | None = None,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Every run's usage as `(started, parsed)` pairs. A run with a `*.usage.json` starts
    where the file says, and a file that fails to parse is skipped, not raised. A
    store-only run starts at its `launched_at` and is skipped without one. With `since`,
    a file last written before it is not opened: it is written when its run ends, so
    that run started before `since`. A store run that ended before `since` is left out too."""
    root = Path(runs_dir)
    usage_files: list[tuple[datetime, dict[str, Any]]] = []
    file_runs: set[str] = set()
    for path in root.glob("*.usage.json"):
        run_id = path.name.removesuffix(".usage.json")
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
        if since is not None and mtime < since:
            file_runs.add(run_id)
            continue
        try:
            usage = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        usage_files.append((_usage_started(usage, mtime), usage))
        file_runs.add(run_id)
    stored = run_store.store_usages(root, exclude=file_runs, since=since.isoformat() if since else None)
    started_at = run_store.runs_started(root) if stored else {}  # one query, not one connection per run
    store_only = [
        (started, usage)
        for run_id, usage in stored.items()
        if (started := _store_started(started_at, run_id)) is not None
    ]
    return usage_files + store_only


def read_usage(
    runs_dir: Path | str, now: datetime, reset: WeeklyReset | None = None,
) -> list[tuple[datetime, dict[str, Any]]]:
    """Every run's usage that can fall inside the weekly window anchored at
    `reset` (rolling 7 days with none), the widest window read."""
    return _read_usage_files(runs_dir, now, since=weekly_window_start(now, reset))


def _cached_blocks(cached: object, now: datetime) -> dict[str, Any] | None:
    """The cached ccusage JSON when it is under `CCUSAGE_CACHE_S` old at `now`, else None.
    A naive or future `at`, or any wrong shape, counts as absent."""
    if not isinstance(cached, dict) or not isinstance(cached.get("blocks"), dict):
        return None
    try:
        at = datetime.fromisoformat(cached["at"])
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        return None
    return cached["blocks"] if 0 <= (now - at).total_seconds() < CCUSAGE_CACHE_S else None


def _read_ccusage_cache(runs_dir: Path | str, now: datetime) -> dict[str, Any] | None:
    try:
        cached = json.loads((Path(runs_dir) / _CCUSAGE_CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _cached_blocks(cached, now)


def _write_ccusage_cache(runs_dir: Path | str, now: datetime, blocks: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        (Path(runs_dir) / _CCUSAGE_CACHE_FILE).write_text(
            json.dumps({"at": now.isoformat(), "blocks": blocks}), encoding="utf-8",
        )


def gather(
    runs_dir: Path | str,
    now: datetime,
    run: Any = subprocess.run,
    window_hours: float = 5.0,
    ceiling_usd: float | None = None,
    usage: list[tuple[datetime, dict[str, Any]]] | None = None,
) -> Window:
    """Impure edge: launches ccusage (or reuses its answer cached under a minute),
    reads the run directory's usage (unless `usage` is given), and folds whichever
    answers into a `Window` via `window_from`."""
    cached = _read_ccusage_cache(runs_dir, now)
    blocks_json: dict[str, Any] = {} if cached is None else cached
    if cached is None:
        try:
            result = run(
                ["npx", "-y", "ccusage@latest", "blocks", "--active", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                blocks_json = json.loads(result.stdout)
                _write_ccusage_cache(runs_dir, now, blocks_json)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
            blocks_json = {}

    return window_from(
        blocks_json, read_usage(runs_dir, now) if usage is None else usage, now, window_hours, ceiling_usd,
    )


def weekly_window_start(now: datetime, reset: WeeklyReset | None) -> datetime:
    """Pure. With no `reset`, the unchanged rolling 7-day cutoff. With one, the
    most recent wall-clock occurrence of `reset.weekday`/`hour`/`minute` in
    `reset.tz` at or before `now`, converted back to `now`'s own tzinfo."""
    if reset is None:
        return now - timedelta(days=7)
    zone = zoneinfo.ZoneInfo(reset.tz)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(hour=reset.hour, minute=reset.minute, second=0, microsecond=0)
    candidate -= timedelta(days=(candidate.weekday() - reset.weekday) % 7)
    if candidate > local_now:
        candidate -= timedelta(days=7)
    return candidate.astimezone(now.tzinfo)


def weekly_window_from(
    usage_files: list[tuple[datetime, dict[str, Any]]],
    now: datetime,
    ceiling_usd: float | None = None,
    reset: WeeklyReset | None = None,
) -> Window:
    """Pure. Same reader as `window_from`'s fallback path, the weekly window
    anchored at `reset` (a rolling 7-day cutoff with none) instead of the
    block start."""
    start = weekly_window_start(now, reset)
    in_window = [usage for ts, usage in usage_files if start <= ts <= now]
    spent_usd = sum(usage_cost_usd(u) for u in in_window)
    elapsed_hours = max((now - start).total_seconds() / 3600, 1e-9)
    return Window(
        start=start, end=now,
        spent_usd=spent_usd, ceiling_usd=ceiling_usd,
        burn_usd_per_hour=spent_usd / elapsed_hours, runs_in_flight=len(in_window),
    )


def weekly_window_from_spend(
    spent_usd: float, now: datetime, ceiling_usd: float | None, reset: WeeklyReset | None = None,
) -> Window:
    """Pure. The `Window` anchored at `reset` for a spend already summed;
    `runs_in_flight` is 0, the store does not count runs."""
    start = weekly_window_start(now, reset)
    elapsed_hours = max((now - start).total_seconds() / 3600, 1e-9)
    return Window(
        start=start, end=now,
        spent_usd=spent_usd, ceiling_usd=ceiling_usd,
        burn_usd_per_hour=spent_usd / elapsed_hours, runs_in_flight=0,
    )


def gather_weekly(
    runs_dir: Path | str,
    now: datetime,
    weekly_ceiling_usd: float | None = None,
    usage: list[tuple[datetime, dict[str, Any]]] | None = None,
    store_spend: Callable[[str], float | None] | None = None,
    reset: WeeklyReset | None = None,
) -> Window:
    """Impure edge. The store wins when `store_spend`, given the ISO text of the anchored
    window start, returns a number; otherwise the usage files win, the same reader as
    `gather` (or the given `usage`), folded through `weekly_window_from`. Either path's
    `Window.start` reflects `reset`."""
    since = weekly_window_start(now, reset)
    spent = None if store_spend is None else store_spend(since.isoformat())
    if spent is not None:
        return weekly_window_from_spend(spent, now, weekly_ceiling_usd, reset)
    return weekly_window_from(
        read_usage(runs_dir, now, reset) if usage is None else usage, now, weekly_ceiling_usd, reset,
    )
