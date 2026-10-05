"""Rows for `cox runs top`: one row per run in flight, built from its events
and its finished trace calls.

Pure: no filesystem, no subprocess, no clock, no curses. The edge derives
`phases` from `<run>:<phase>.json` manifest names and `calls` from each
trace file's `result` record; this module only ever sees plain data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_tools.events import Event

__all__ = ["UNSET", "Row", "chair_highlight", "column_widths", "highlight", "label", "order", "render", "row", "tail_lines"]

_COLUMNS = ("MACHINE", "PHASE", "NODE", "ATT", "TURNS", "COST", "VERDICT", "STATUS", "CEIL", "BY")
_HEADERS = ("RUN", *_COLUMNS)
_RIGHT = {"ATT", "TURNS", "COST"}
_NO_RUNS = "all lanes clear"
STALL_S = 90  # a live run whose lease heartbeat is this many seconds old reads as stalled

UNSET = object()  # `render`'s "no chair argument given" default, distinct from a real `None` (no lock file).


@dataclass(frozen=True)
class Row:
    run: str
    alive: bool
    phase: str
    node: str
    attempt: int
    turns: int
    cost_usd: float
    verdict: str
    status: str
    ceiling: str = ""
    launched_by: str = ""
    heartbeat_age: int | None = None
    node_call_stalled: bool = False
    host: str | None = None
    remote: bool = False
    short_id: str = ""


def label(r: Row) -> str:
    """The RUN column's text: the short id when the row has one, else the run key. `r.run` stays the key."""
    return r.short_id or r.run


def _status(events: list[Event], alive: bool, orphaned: bool = False, heartbeat_age: int | None = None,
            node_call_stalled: bool = False) -> str:
    if any(e.kind == "task_quarantined" for e in events):
        return "quarantined"
    if any(e.kind == "budget_stop" for e in events):
        return "budget"
    if not alive or any(e.kind == "run_exited" for e in events):
        return "exited"
    if orphaned:
        return "orphaned"
    if (heartbeat_age is not None and heartbeat_age >= STALL_S) or node_call_stalled:
        return "stalled"
    return "running"


def _ceiling_label(ceiling: dict | None) -> str:
    """Pure: the CEIL column's text for a run's parsed ceiling.json (see
    records.ceiling_for), or "" when the run carried no tier/effort overlay."""
    applied = (ceiling or {}).get("applied") or {}
    tier, effort = applied.get("tier") or "", applied.get("effort") or ""
    return "/".join(part for part in (tier, effort) if part)


def _orphaned(alive: bool, launched_by: str, chair: dict | None) -> bool:
    """Alive, and either the chair lost its heartbeat, or a known launcher is not the live chair; no chair file is not an alert."""
    if not alive or chair is None:
        return False
    if chair.get("state") in ("stale", "crashed"):
        return True
    return bool(launched_by) and chair.get("state") == "live" and launched_by != chair.get("holder")


def row(run: str, alive: bool, phases: list[str], events: list[Event], calls: list[dict], ceiling: dict | None = None,
        launched_by: str = "", chair: dict | None = None, heartbeat_age: int | None = None, host: str | None = None,
        node_call_stalled: bool = False, short_id: str = "") -> Row:
    """Pure: the one row a run's events and finished calls make; `host` is the machine it runs on."""
    starts = [e for e in events if e.kind == "node_started"]
    verdicts = [e for e in events if e.kind == "verdict"]
    node = starts[-1].detail["node"] if starts else ""
    attempt = starts[-1].detail["attempt"] if starts else 0
    verdict = verdicts[-1].detail["verdict"] if verdicts else ""
    return Row(
        run=run,
        alive=alive,
        phase=phases[-1] if phases else "",
        node=node,
        attempt=attempt,
        turns=sum(c["turns"] for c in calls),
        cost_usd=sum(c["cost_usd"] for c in calls),
        verdict=verdict,
        status=_status(events, alive, _orphaned(alive, launched_by, chair), heartbeat_age, node_call_stalled),
        ceiling=_ceiling_label(ceiling),
        launched_by=launched_by,
        heartbeat_age=heartbeat_age,
        node_call_stalled=node_call_stalled,
        host=host,
        short_id=short_id,
    )


def remote_row(run: str, host: str | None, heartbeat_age: int | None, calls: list[dict] = (), phase: str = "",
               short_id: str = "") -> Row:
    """Pure: the row for a live lane no local pidfile names. Its events and verdicts are on the other machine, so the
    lease gives the status and the store's `calls` (the `_call` shape) give node, attempt, turns and cost.
    A run with no call recorded yet reads node `starting`, never a blank."""
    last = calls[-1] if calls else None
    return Row(run=run, alive=True, phase=phase,
               node=last["node"] if last else "starting", attempt=last["attempt"] if last else 0,
               turns=sum(c["turns"] for c in calls), cost_usd=round(sum(c["cost_usd"] for c in calls), 4), verdict="",
               status=_status([], True, False, heartbeat_age), heartbeat_age=heartbeat_age, host=host, remote=True,
               short_id=short_id)


def _cut(line: str, width: int) -> str:
    return line[: max(width, 0)]


def tail_lines(text: str, limit: int) -> list[str]:
    """Pure: the last `limit` lines of `text`."""
    lines = text.splitlines()
    return lines[-limit:] if limit > 0 else []


def order(rows: list) -> list:
    return sorted(rows, key=lambda r: (not r.alive, r.run))


def _chair_line(chair: dict | None) -> str:
    if chair is None:
        return "chair: none"
    return f"chair: {chair['holder']} ({chair['state']}, beat {chair['minutes_ago']}m ago)"


def _cells(r: Row) -> tuple[str, ...]:
    return (label(r), r.host or "?", r.phase, r.node, str(r.attempt), str(r.turns), f"${r.cost_usd:.2f}", r.verdict, r.status, r.ceiling, r.launched_by)


def column_widths(rows: Sequence[Row], headers: Sequence[str]) -> tuple[int, ...]:
    """Pure: per column, the longer of its header and every rendered cell."""
    cells = [_cells(r) for r in rows]
    return tuple(max([len(h), *(len(row[i]) for row in cells)]) for i, h in enumerate(headers))


def _padded(cell: str, header: str, w: int) -> str:
    return cell.rjust(w) if header in _RIGHT else cell.ljust(w)


def render(rows: list[Row], width: int, chair=UNSET, expanded=None, detail_lines: tuple = ()) -> list[str]:
    """Pure: the chair line (only when `chair` is passed), the header line,
    one line per row, and `detail_lines` indented two spaces under `expanded`'s
    row (nothing, if `expanded` names no row in `rows`), all cut to `width`."""
    prefix = [] if chair is UNSET else [_chair_line(chair)]
    widths = column_widths(rows, _HEADERS)
    header = " ".join(_padded(h, h, w) for h, w in zip(_HEADERS, widths))
    if not rows:
        return [_cut(line, width) for line in [*prefix, header, _NO_RUNS]]
    ordered = order(rows)
    body = []
    for r in ordered:
        body.append(" ".join(_padded(c, h, w) for c, h, w in zip(_cells(r), _HEADERS, widths)))
        if r.run == expanded:
            body.extend("  " + line for line in detail_lines)
    return [_cut(line, width) for line in [*prefix, header, *body]]


def highlight(row: Row) -> str:
    """Pure: the label the screen maps to a colour. Names no colour itself."""
    if row.status in ("quarantined", "budget", "orphaned"):
        return "alert"
    if row.status == "exited":
        return "dim"
    return "normal"


def chair_highlight(chair: dict | None) -> str:
    """Pure: the label the screen maps to a colour for the chair line itself."""
    return "alert" if chair is not None and chair.get("state") in ("stale", "crashed") else "normal"
