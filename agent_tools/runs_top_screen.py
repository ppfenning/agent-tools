"""Curses edge for `cox runs top`: reads the facts a running harness leaves on
disk, maps them through the pure `runs_top` model, and draws the result.
`curses` is imported inside each function that needs it, so this module
imports on a machine with no terminal and the pure pieces stay testable with
a fake stdscr.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import re
import socket
import time
from pathlib import Path

from agent_tools import chair, chair_stall, epic, run_store, runs_top
from agent_tools import events as events_module
from agent_tools.records import ceiling_for, load_trace

__all__ = ["chair_now", "draw", "facts", "first_visible", "loop", "main", "rows_now"]

_STALE_SECONDS = 600
_TRACE_NAME = re.compile(r"^([A-Za-z0-9_]+)-(\d+)$")


def is_alive(now_alive, pid: int, pidfile: Path) -> bool:
    """Edge. An injected `now_alive(pid)` probe when given; otherwise `epic.run_live`, so the screen agrees with route status."""
    return epic.run_live(pid, pidfile) if now_alive is None else now_alive(pid)


def _read_pid(path: Path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _phases(root: Path, run: str) -> list[str]:
    return run_store.phase_names(root, run)


def _call(path: Path) -> dict | None:
    """The `result` line's cost and turns for one trace file, or None when
    the name does not fit `<node>-<n>.jsonl` or the file has no result line.
    A file that fails to parse is skipped by `load_trace`, never raised."""
    m = _TRACE_NAME.match(path.stem)
    if not m:
        return None
    try:
        trace_events = load_trace(path)
    except OSError:
        return None
    result = next((e for e in trace_events if e.get("type") == "result"), None)
    if result is None:
        return None
    return {"node": m.group(1), "attempt": int(m.group(2)),
            "cost_usd": result.get("total_cost_usd", 0.0), "turns": result.get("num_turns", 0),
            "ts": _mtime_ts(path)}


def _mtime_ts(path: Path) -> str | None:
    """When a trace file was last written, as `%Y-%m-%dT%H:%M:%SZ`. The `result` line carries no timestamp of its own."""
    try:
        written = path.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(written, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def calls_from_usage(usage_calls: list[dict]) -> list[dict]:
    """Stored calls in order as `_call` rows; `attempt` counts earlier calls with the same role."""
    roles = [str(c.get("role") or "unknown") for c in usage_calls]
    return [
        {"node": role, "attempt": 1 + roles[:i].count(role),
         "cost_usd": c.get("cost_usd", 0.0), "turns": c.get("turns", 0), "ts": c.get("ts")}
        for i, (c, role) in enumerate(zip(usage_calls, roles))
    ]


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []


def _ceiling(root: Path, run: str) -> dict | None:
    path = root / f"{run}.ceiling.json"
    if not path.exists():
        return None
    return ceiling_for(run, {path.name: path.read_text(encoding="utf-8")})


def _launched_by(root: Path, run: str) -> str:
    path = root / f"{run}.launched.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return data.get("launched_by", "") if isinstance(data, dict) else ""


def _written_at(path: Path) -> tuple[float, str]:
    """When a trace was last written, name breaking a tie: the node running NOW is the one
    written last, which alphabetical order does not know."""
    try:
        return path.stat().st_mtime, path.name
    except OSError:
        return 0.0, path.name


def _heartbeat_age(lease: tuple[str, str, str] | None, run: str, now: datetime.datetime) -> int | None:
    """Seconds since the lease heartbeat; None when the lease is absent, held by another run, or unreadable."""
    if lease is None or lease[0] != run:
        return None
    try:
        return max(int((now - datetime.datetime.fromisoformat(lease[2])).total_seconds()), 0)
    except (TypeError, ValueError):
        return None


def _stall_ts(value: str | None) -> str | None:
    """An ISO timestamp reformatted to `chair_stall`'s `%Y-%m-%dT%H:%M:%SZ`; None when absent or unparseable."""
    if value is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.UTC)
    return aware.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _node_call_stalled(calls: list[dict], started_at: str | None, now: datetime.datetime) -> bool:
    """True once the newest of the already-loaded `calls` (or the run's own start, with none yet) and the
    run's start both clear `chair_stall.STALL_MINUTES`; False outright with no stored `started_at`."""
    if started_at is None:
        return False
    newest_ts = _stall_ts(calls[-1]["ts"]) if calls else None
    idle = chair_stall.idle_minutes(newest_ts, started_at, now)
    started = chair_stall.started_minutes(started_at, now)
    return chair_stall.is_stalled(idle, started)


def _fact(root: Path, run: str, alive: bool, now: datetime.datetime | None = None) -> dict:
    now = now or datetime.datetime.now(datetime.UTC)
    lines = _read_lines(root / f"{run}.log")
    trace_dir = root / f"{run}-trace"
    trace_paths = sorted(trace_dir.glob("*.jsonl"), key=_written_at) if trace_dir.exists() else []
    stored = [] if trace_paths else calls_from_usage((run_store.usage(root, run) or {}).get("calls") or [])
    names = [p.name for p in trace_paths] or [f"{c['node']}-{c['attempt']}.jsonl" for c in stored]
    events = events_module.from_log(run, lines) + events_module.from_trace_names(run, names)
    calls = [c for c in (_call(p) for p in trace_paths) if c is not None] or stored
    started_at = _stall_ts(run_store.run_started(root, run))
    return {"run": run, "alive": alive, "phases": _phases(root, run), "events": events, "calls": calls,
            "ceiling": _ceiling(root, run), "launched_by": _launched_by(root, run),
            "heartbeat_age": _heartbeat_age(run_store.lease(root, run), run, now),
            "node_call_stalled": _node_call_stalled(calls, started_at, now)}


def facts(runs_dir, now_alive=None) -> list[dict]:
    """Every run still worth a line on screen: a `.pid` that probes alive,
    plus a `.log` whose `.pid` is missing or dead but was touched in the
    last ten minutes, so a run that just exited stays on screen briefly."""
    root = Path(runs_dir)
    pids = {p.stem: _read_pid(p) for p in root.glob("*.pid")}
    alive = {run for run, pid in pids.items() if pid is not None and is_alive(now_alive, pid, root / f"{run}.pid")}
    now = time.time()
    recent = {
        p.stem for p in root.glob("*.log")
        if p.stem not in alive and now - p.stat().st_mtime <= _STALE_SECONDS
    }
    return [_fact(root, run, run in alive) for run in sorted(alive | recent)]


def _minutes_ago(heartbeat_at, now: datetime.datetime) -> int:
    try:
        beat = datetime.datetime.fromisoformat(heartbeat_at)
    except (TypeError, ValueError):
        return 0
    return max(int((now - beat).total_seconds() // 60), 0)


def chair_now(runs_dir, heartbeat_minutes: int = chair.DEFAULT_HEARTBEAT_MINUTES, pid_alive=chair.pid_alive) -> dict | None:
    """Edge: `runs/chair.json` turned into the plain dict `runs_top.render` shows,
    or None when no lock is held. A file present but unreadable reads as no lock,
    same as `chair.read`'s own contract for a missing file."""
    try:
        record = chair.read(runs_dir)
    except (OSError, json.JSONDecodeError):
        record = None
    if record is None:
        return None
    now = datetime.datetime.now(datetime.UTC)
    alive = pid_alive(record["pid"]) if isinstance(record.get("pid"), int) else False
    state = chair.liveness(record, alive, now, socket.gethostname(), heartbeat_minutes)
    return {"holder": record.get("session", ""), "state": state, "minutes_ago": _minutes_ago(record.get("heartbeat_at"), now)}


def _lane_age(heartbeat_at: str, now: datetime.datetime) -> int | None:
    """Seconds since a lane's heartbeat; None when the timestamp is unreadable."""
    try:
        return max(int((now - datetime.datetime.fromisoformat(heartbeat_at)).total_seconds()), 0)
    except (TypeError, ValueError):
        return None


def _remote_rows(root: Path, lanes: list[run_store.Lane], now: datetime.datetime) -> list[runs_top.Row]:
    """Edge. One row per live lane no local pidfile names. Reads no log, trace or lease of that run: its files are on the other machine."""
    remote = run_store.remote_lanes(lanes, {p.stem for p in root.glob("*.pid")})
    return [runs_top.remote_row(lane.run, lane.host, _lane_age(lane.heartbeat_at, now)) for lane in remote]


def rows_now(runs_dir, heartbeat_minutes: int = chair.DEFAULT_HEARTBEAT_MINUTES,
             now: datetime.datetime | None = None) -> list[runs_top.Row]:
    now = now or datetime.datetime.now(datetime.UTC)
    root = Path(runs_dir)
    chair_state = chair_now(runs_dir, heartbeat_minutes)
    host = socket.gethostname()
    local = [runs_top.row(f["run"], f["alive"], f["phases"], f["events"], f["calls"], f["ceiling"], f["launched_by"], chair_state,
                          f["heartbeat_age"], host, f["node_call_stalled"])
             for f in facts(runs_dir)]
    lanes = run_store.live_lanes(root, now.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return [*local, *_remote_rows(root, lanes, now)]


def _has_colors() -> bool:
    import curses

    try:
        return curses.has_colors()
    except curses.error:
        return False  # no real terminal behind stdscr, e.g. under test


def _attr(row, has_color: bool):
    import curses

    label = runs_top.highlight(row)
    if label == "alert":
        return (curses.color_pair(1) if has_color else 0) | curses.A_BOLD
    if label == "dim":
        return curses.A_DIM
    return curses.A_NORMAL


def _chair_attr(chair_state, has_color: bool):
    import curses

    if runs_top.chair_highlight(chair_state) == "alert":
        return (curses.color_pair(1) if has_color else 0) | curses.A_BOLD
    return curses.A_NORMAL


def _ordered(rows: list) -> list:
    return runs_top.order(rows)


def _line_kinds(ordered: list, expanded, detail_count: int, has_leader: bool) -> list:
    """One entry per line `runs_top.render` draws: `None` for the leader line
    (when `has_leader`), the header, or a detail line, `(row, index)` for a
    row's own line."""
    kinds = [None] * (2 if has_leader else 1)
    for i, r in enumerate(ordered):
        kinds.append((r, i))
        if r.run == expanded:
            kinds.extend([None] * detail_count)
    return kinds


def _scroll_facts(ordered: list, expanded, detail_count: int, cursor: int, has_leader: bool) -> tuple[int, int]:
    """The cursor's absolute line number and the total line count, for `first_visible`."""
    kinds = _line_kinds(ordered, expanded, detail_count, has_leader)
    cursor_line = next(i for i, k in enumerate(kinds) if k is not None and k[1] == cursor)
    return cursor_line, len(kinds)


def first_visible(cursor_index: int, total_lines: int, window_height: int, current_first: int) -> int:
    """Pure: the scroll offset that keeps `cursor_index` on screen, moving `current_first` no more than it must."""
    if window_height <= 0:
        return current_first
    last_first = max(total_lines - window_height, 0)
    first = min(current_first, last_first)
    if cursor_index < first:
        first = cursor_index
    elif cursor_index > first + window_height - 1:
        first = cursor_index - window_height + 1
    return max(min(first, last_first), 0)


def draw(stdscr, rows: list, cursor: int | None = None, chair_state=runs_top.UNSET,
         expanded=None, detail_lines: tuple = (), first: int = 0) -> None:
    import curses

    stdscr.clear()
    height, width = stdscr.getmaxyx()
    lines = runs_top.render(rows, width, chair_state, expanded, detail_lines)
    has_leader = chair_state is not runs_top.UNSET
    ordered = _ordered(rows)
    kinds = _line_kinds(ordered, expanded, len(detail_lines), has_leader)
    has_color = _has_colors()
    if has_color:
        try:
            curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
        except curses.error:
            has_color = False
    for row_i, line in enumerate(lines[first:first + height]):
        i = first + row_i
        if has_leader and i == 0:
            attr = _chair_attr(chair_state, has_color)
        else:
            kind = kinds[i] if 0 <= i < len(kinds) else None
            base = _attr(kind[0], has_color) if kind is not None else curses.A_NORMAL
            attr = base | curses.A_REVERSE if kind is not None and cursor == kind[1] else base
        with contextlib.suppress(curses.error):
            stdscr.addnstr(row_i, 0, line, width, attr)
    stdscr.refresh()


def _session_text(root: Path, run: str) -> str:
    """Edge: the newest trace file's assistant message text, one line per text item.
    Tool calls are left out; `facts_for`'s `tail` already carries those into the
    detail's own `last:` line, so repeating them here would print them twice."""
    from agent_tools import runs_detail_screen

    newest = runs_detail_screen._newest_trace(root, run)
    if newest is None:
        return ""
    lines = [
        item.get("text", "")
        for event in load_trace(newest)
        for item in runs_detail_screen._content(event)
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n".join(lines)


def _accordion_detail(runs_dir, run: str, width: int, now_alive) -> list[str]:
    from agent_tools import runs_detail, runs_detail_screen

    root = Path(runs_dir)
    detail = runs_detail.detail(**runs_detail_screen.facts_for(runs_dir, run, now_alive=now_alive))
    tail = runs_top.tail_lines(_session_text(root, run), 3)
    return [*runs_detail.render(detail, width), *tail]


def loop(stdscr, runs_dir, interval: float, tick=rows_now, now_alive=None,
         heartbeat_minutes: int = chair.DEFAULT_HEARTBEAT_MINUTES) -> int:
    import curses

    with contextlib.suppress(curses.error):  # no real terminal behind stdscr, e.g. under test
        curses.curs_set(0)
    stdscr.timeout(int(interval * 1000))
    cursor = 0
    expanded = None
    first = 0
    while True:
        rows = tick(runs_dir)
        chair_state = chair_now(runs_dir, heartbeat_minutes)
        ordered = _ordered(rows)
        cursor = min(cursor, len(ordered) - 1) if ordered else 0
        expanded = expanded if any(r.run == expanded for r in ordered) else None
        height, width = stdscr.getmaxyx()
        is_remote = any(r.run == expanded and r.remote for r in ordered)  # its files are on another machine
        detail_lines = _accordion_detail(runs_dir, expanded, width, now_alive) if expanded is not None and not is_remote else ()
        if ordered:
            cursor_line, total_lines = _scroll_facts(ordered, expanded, len(detail_lines), cursor,
                                                       chair_state is not runs_top.UNSET)
            first = first_visible(cursor_line, total_lines, height, first)
        draw(stdscr, rows, cursor if ordered else None, chair_state, expanded, detail_lines, first)
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            return 0
        if ordered and ch in (ord("j"), curses.KEY_DOWN):
            cursor = min(cursor + 1, len(ordered) - 1)
        elif ordered and ch in (ord("k"), curses.KEY_UP):
            cursor = max(cursor - 1, 0)
        elif ordered and ch in (10, 13, curses.KEY_ENTER):
            run = ordered[cursor].run
            expanded = None if expanded == run else run
        elif ch == 27:
            expanded = None
        # curses.KEY_RESIZE and a plain timeout both fall through here: either
        # way the next iteration redraws against the current rows and size.


def main(runs_dir, interval: float, heartbeat_minutes: int = chair.DEFAULT_HEARTBEAT_MINUTES) -> int:
    import curses
    import signal

    def _hangup(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGHUP, _hangup)
    try:
        return curses.wrapper(lambda stdscr: loop(stdscr, runs_dir, interval, heartbeat_minutes=heartbeat_minutes))
    except KeyboardInterrupt:
        # A closed window sends SIGHUP; raising through the wrapper lets it
        # restore the terminal before this function returns.
        return 0
    finally:
        signal.signal(signal.SIGHUP, previous)
