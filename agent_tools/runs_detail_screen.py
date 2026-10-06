"""Edge for `cox runs detail`: gathers what `runs_detail.detail` takes."""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from pathlib import Path

from agent_tools import run_store, runs_top_screen
from agent_tools.records import load_trace

__all__ = ["facts_for"]


def _record(root: Path, run: str) -> dict:
    """Missing, torn or unreadable reads as `{}`, never a raise."""
    matches = sorted((root / run / "tasks").glob("*/*.json"))
    if not matches:
        return {}
    try:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _content(event: dict) -> list:
    """A trace line's tool-use items, or none: `message` is a string on some lines and a mapping on others."""
    message = event.get("message") if isinstance(event, dict) else None
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return content if isinstance(content, list) else []


def _tool_names(trace_events: list[dict]) -> list[str]:
    return [
        item["name"]
        for event in trace_events
        for item in _content(event)
        if isinstance(item, dict) and item.get("type") == "tool_use"
    ]


def _newest_trace(root: Path, run: str) -> Path | None:
    trace_dir = root / f"{run}-trace"
    paths = [p for p in trace_dir.glob("*.jsonl") if runs_top_screen._TRACE_NAME.match(p.stem)] if trace_dir.exists() else []
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def _tail(root: Path, run: str) -> list[str]:
    newest = _newest_trace(root, run)
    if newest:
        return _tool_names(load_trace(newest))[-3:]
    calls = (run_store.usage(root, run) or {}).get("calls") or []
    if not calls:
        return []
    try:
        events = run_store.call_events(root, run, calls[-1])
    except run_store.TracesUnavailable:
        return []
    return _tool_names(events or [])[-3:]


def _live_runs(root: Path) -> set[str]:
    """Edge. Runs a live `runs:` lease names, read as of now."""
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {lane.run for lane in run_store.live_lanes(root, now)}


def facts_for(runs_dir, run: str, now_alive=None, *, live_runs: Callable[[], set[str]] | None = None) -> dict:
    """`now_alive` is the same pid-probe seam as `runs_top_screen.facts`. With no pidfile, a live lease naming the run counts it alive; `live_runs` is that seam."""
    root = Path(runs_dir)
    pid = runs_top_screen._read_pid(root / f"{run}.pid")
    if pid is None:
        alive = run in (live_runs or (lambda: _live_runs(root)))()
    else:
        alive = runs_top_screen.is_alive(now_alive, pid, root / f"{run}.pid")
    fact = runs_top_screen._fact(root, run, alive)
    return {
        "run": run,
        "alive": alive,
        "events": fact["events"],
        "calls": fact["calls"],
        "record": _record(root, run),
        "tail": _tail(root, run),
    }
