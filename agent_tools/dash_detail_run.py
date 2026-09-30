"""The data behind `cox dash --detail run <run_id>`: the same two readers
`runs_top_screen._accordion_detail` composes for `cox runs top`'s drill-down.
`facts_for` plus `runs_detail.detail` give the timeline, verdicts, arbiter
reasoning, files and tool calls. `_session_text` gives the log tail: the newest
trace's session text, which leaves tool calls out so they are not repeated.

`build_run_detail` is the pure core one step further on: given a `raw` dict
carrying `build`'s own six keys (`timeline`, `verdicts`, `arbiter`, `files`,
`tool_calls`, `log_tail`) plus the run's own identifying fields (`run`,
`machine`, `initiative`, `phase`), an `alive` flag, `task` (the run's current
task id), `log_lines` (the run log's own lines, read whole so a leading
quarantine line is not lost the way `log_tail`'s last three lines would lose
it) and `fix_loop_stopped` (that current task's own `fix_loop.stopped` value, read from
`<runs_dir>/<run>/tasks/<phase>/<task>.json`), it reshapes them into the
schema-1 `cox dash --detail run` snapshot fixed by
tests/fixtures/dash_detail_run_v1.json. It reads no file and takes no run id
of its own; wiring `build`'s edge output into that richer `raw` shape,
including reading `log_lines` and `fix_loop_stopped` off disk, is later work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import events, run_store, runs_detail, runs_top
from agent_tools.runs_detail_screen import facts_for
from agent_tools.runs_top_screen import _session_text

__all__ = ["build", "build_run_detail"]


def _node_call(call: runs_detail.NodeCall) -> dict:
    return {
        "node": call.node,
        "attempt": call.attempt,
        "turns": call.turns,
        "cost_usd": call.cost_usd,
        "verdict": call.verdict,
    }


def build(run_id: str, runs_dir: Path, now: str) -> dict:
    """Plain dict, never a dataclass or `Path`; `now` decides a pidless run's liveness."""
    root = Path(runs_dir)
    facts = facts_for(root, run_id, live_runs=lambda: {lane.run for lane in run_store.live_lanes(root, now)})
    detail = runs_detail.detail(**facts)
    timeline = [_node_call(call) for call in detail.timeline]
    return {
        "timeline": timeline,
        "verdicts": [entry for entry in timeline if entry["verdict"]],
        "arbiter": detail.objection,
        "files": list(detail.files_touched),
        "tool_calls": list(detail.last_calls),
        "log_tail": runs_top.tail_lines(_session_text(root, run_id), 3),
    }


_STEP_ORDER = ("plan", "build", "handoff", "review", "arbitrate", "land")

_NODE_TO_STEP = {
    "plan": "plan",
    "scope_epic": "plan",
    "build": "build",
    "validate_chunk": "build",
    "handoff": "handoff",
    "review_charter": "review",
    "review_adversary": "review",
    "arbitrate": "arbitrate",
    "validate_phase": "land",
    "land": "land",
}


def _folded_steps(timeline: list[dict]) -> dict[str, dict]:
    """Raw timeline entries grouped into the six pipeline steps: `turns` and `cost_usd` summed
    per step, `verdict` the last folded entry's own (null when none had one or the step folded
    none), `touched` marking a step that folded at least one raw entry, for `_statuses` to find
    the run's current step."""
    grouped = {step: [e for e in timeline if _NODE_TO_STEP.get(e.get("node")) == step] for step in _STEP_ORDER}
    return {
        step: {
            "turns": sum(e.get("turns", 0) for e in entries),
            "cost": round(sum(e.get("cost_usd", 0.0) for e in entries), 6),
            "verdict": (entries[-1].get("verdict") or None) if entries else None,
            "touched": bool(entries),
        }
        for step, entries in grouped.items()
    }


def _statuses(folded: dict[str, dict], alive: bool) -> dict[str, str]:
    """`done` before the run's current step (the last step to fold any raw entry), `running` at
    that step only while the run is `alive`, `pending` there and beyond otherwise; a run that
    folded nothing yet has no current step, so every step reads `pending`."""
    touched = [index for index, step in enumerate(_STEP_ORDER) if folded[step]["touched"]]
    current = max(touched) if touched else -1
    return {
        step: "done" if index < current else ("running" if index == current and alive else "pending")
        for index, step in enumerate(_STEP_ORDER)
    }


def _stopped_reason(raw: dict) -> str | None:
    """The last `quarantined task: <task> — <reason>` line naming `raw["task"]`, else that task's
    `fix_loop_stopped`, else null. A quarantine line for any other task is never this run's reason."""
    task = raw.get("task")
    quarantines = [
        e.detail["reason"]
        for e in events.from_log(raw.get("run", ""), raw.get("log_lines") or [])
        if e.kind == "task_quarantined" and task and e.detail["task"] == task
    ]
    return quarantines[-1] if quarantines else (raw.get("fix_loop_stopped") or None)


def _tool_call(entry: Any) -> dict:
    """A raw `tool_calls` entry as `{tool, summary, at}`: a mapping that already distinguishes
    them passes through, a missing `at` read as `""`; an opaque string splits on the first
    `": "` into `tool` and `summary`, or becomes an all-`summary` entry with `tool: ""` when it
    carries no `": "` at all. Neither shape carries a timestamp of its own unless a mapping
    supplies `at`, so a bare string always reads `at: ""`."""
    if isinstance(entry, dict):
        return {"tool": entry.get("tool", ""), "summary": entry.get("summary", ""), "at": entry.get("at") or ""}
    text = str(entry)
    tool, sep, summary = text.partition(": ")
    return {"tool": tool, "summary": summary, "at": ""} if sep else {"tool": "", "summary": text, "at": ""}


def build_run_detail(raw: dict) -> dict:
    """The schema-1 `cox dash --detail run` snapshot for a `raw` dict carrying `build`'s own six
    keys plus `run`, `machine`, `initiative`, `phase`, `alive`, `task`, `log_lines` and
    `fix_loop_stopped`. `raw` carries no clock of its own, so `at` is the one wall-clock read in
    this otherwise pure mapping."""
    folded = _folded_steps(raw.get("timeline") or [])
    statuses = _statuses(folded, bool(raw.get("alive")))
    steps = [
        {
            "node": step,
            "turns": folded[step]["turns"],
            "cost": folded[step]["cost"],
            "verdict": folded[step]["verdict"],
            "status": statuses[step],
        }
        for step in _STEP_ORDER
    ]
    return {
        "schema": 1,
        "kind": "run",
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": raw.get("run"),
        "machine": raw.get("machine"),
        "initiative": raw.get("initiative"),
        "phase": raw.get("phase"),
        "steps": steps,
        "stopped_reason": _stopped_reason(raw),
        "files": list(raw.get("files") or []),
        "last_tool_calls": [_tool_call(entry) for entry in raw.get("tool_calls") or []],
        "log_tail": list(raw.get("log_tail") or []),
    }
