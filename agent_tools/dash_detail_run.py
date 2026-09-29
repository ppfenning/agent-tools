"""The data behind `cox dash --detail run <run_id>`: the same two readers
`runs_top_screen._accordion_detail` composes for `cox runs top`'s drill-down.
`facts_for` plus `runs_detail.detail` give the timeline, verdicts, arbiter
reasoning, files and tool calls. `_session_text` gives the log tail: the newest
trace's session text, which leaves tool calls out so they are not repeated.
"""

from __future__ import annotations

from pathlib import Path

from agent_tools import run_store, runs_detail, runs_top
from agent_tools.runs_detail_screen import facts_for
from agent_tools.runs_top_screen import _session_text

__all__ = ["build"]


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
