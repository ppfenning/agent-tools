"""Curses edge for `cox console`: gathers drafts, hosts (the local machine
always included, lanes in use over capacity), lanes, the chair's live/stale
state and the spend header, renders them, and runs the `cox` command
`console_plan.plan_command` plans for a keypress. `curses` is imported
inside each function that needs it, so this module imports on a machine
with no terminal and the pure pieces (`gather`, `render`, `selection_at`,
`with_local_host`) stay testable without one. `console_screen` never reads
usage, pacing or the run store's cost directly: the CLI edge computes the
spend figures and hands them in as a plain dict.
"""

from __future__ import annotations

import contextlib
import functools
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from agent_tools import chair_facts, chair_read_stale, console_plan, draft_list, run_store, runs_top_screen

_SECTION_ORDER = ("drafts", "hosts", "lanes", "chair", "needs_chair")
_SELECTABLE_SECTIONS = ("drafts", "hosts", "lanes", "chair")
_NEEDS_CHAIR_KIND = "needs_chair"
_NEEDS_CHAIR_WINDOW_S = 600
_LANDED_STATES = ("done", "dropped")
_PHASE_SNAPSHOT_S = 30  # one read of work_items serves every lane redraw in this window: the harness subprocess it runs has a 60s timeout


def _parse_iso(value: object) -> datetime | None:
    """The ISO-8601 `value` as a UTC-aware `datetime`; None when it is not a string or not parseable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_recent_needs_chair(row: dict, now: datetime) -> bool:
    """A `needs_chair` action row whose `ts` is within `_NEEDS_CHAIR_WINDOW_S` seconds of `now`, either side."""
    if row.get("kind") != _NEEDS_CHAIR_KIND:
        return False
    ts = _parse_iso(row.get("ts"))
    return ts is not None and abs((now - ts).total_seconds()) <= _NEEDS_CHAIR_WINDOW_S


def _action(row: dict) -> dict:
    """The row's `action_json` as a dict: Postgres hands back a dict, SQLite a JSON string."""
    raw = row.get("action_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _needs_chair_key(row: dict) -> tuple[str, str]:
    action = _action(row)
    return str(action.get("initiative") or row.get("target") or "?"), str(action.get("cause") or row.get("cause") or "?")


def newest_per_item(rows: list[dict]) -> list[dict]:
    """Pure. One row per (initiative, cause), the newest by `ts`: the loop records the same item every tick."""
    newest: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = _needs_chair_key(row)
        if key not in newest or str(row.get("ts") or "") > str(newest[key].get("ts") or ""):
            newest[key] = row
    return sorted(newest.values(), key=_needs_chair_key)


def with_local_host(hosts: list[dict], local_name: str, local_capacity: int) -> list[dict]:
    """Pure. `hosts` unchanged when a row already names `local_name`; otherwise `hosts` plus a synthetic active
    row for it, so the local machine always has a row even before `cox host beat` first writes one."""
    if any(row.get("name") == local_name for row in hosts):
        return hosts
    return [*hosts, {"name": local_name, "capacity": local_capacity, "state": "active"}]


def _lanes_in_use(lanes: list, local_name: str) -> dict[str, int]:
    """Pure. Live lane count per host name; a lane with no host, or one on `local_name`, counts under
    `local_name` — a lane launched on this machine carries no `host` value of its own."""
    names = [local_name if lane.host in (None, local_name) else lane.host for lane in lanes]
    return {name: names.count(name) for name in dict.fromkeys(names)}


def _hosts_with_lane_counts(hosts: list[dict], lanes: list, local_name: str) -> list[dict]:
    """Pure. `hosts` with each row's `in_use` set from `lanes`, so `_host_line` can show lanes in use over capacity."""
    in_use = _lanes_in_use(lanes, local_name)
    return [{**row, "in_use": in_use.get(row.get("name"), 0)} for row in hosts]


@functools.lru_cache(maxsize=8)
def _work_items_snapshot(runs_dir: str, _window: int) -> tuple[dict, ...]:
    """Every `work_items` row, read once per `_PHASE_SNAPSHOT_S` window: `loop` calls `gather` on every tick and
    after every keypress, and `run_store.work_items` starts a harness interpreter with a 60-second timeout."""
    return tuple(run_store.work_items(Path(runs_dir)))


def _phase_progress(items: Sequence[dict], initiative: str) -> tuple[int, int]:
    """Pure. `(landed, total)` phases of `initiative`: a phase is landed when every one of its items is `done`
    or `dropped`. A count needs no order, so this never looks at which item came first."""
    by_phase: dict[str, list[str]] = {}
    for row in items:
        if row.get("initiative") == initiative:
            by_phase.setdefault(row.get("phase"), []).append(row.get("state"))
    landed = sum(1 for states in by_phase.values() if states and all(s in _LANDED_STATES for s in states))
    return landed, len(by_phase)


@dataclass(frozen=True)
class LaneRow:
    """A lane joined to the `cox runs top` columns of its own run and its initiative's phase progress."""

    run: str
    host: str | None
    heartbeat_at: str
    phase: str
    node: str
    attempt: int
    turns: int
    cost_usd: float
    phases_landed: int
    phases_total: int


def _lane_rows(lanes: list, run_rows: dict, items: Sequence[dict]) -> list[LaneRow]:
    """Pure. One `LaneRow` per lane. `phase`/`node`/`attempt`/`turns`/`cost_usd` are the run's own `rows_now`
    row's values (blank/zero when it has none yet) — never a value read off the order of `run_rows` or `items`.
    `phases_landed`/`phases_total` come from `_phase_progress` on the run's own initiative."""
    rows = []
    for lane in lanes:
        run_row = run_rows.get(lane.run)
        landed, total = _phase_progress(items, chair_facts.run_initiative(lane.run))
        rows.append(LaneRow(
            run=lane.run,
            host=lane.host,
            heartbeat_at=lane.heartbeat_at,
            phase=run_row.phase if run_row is not None else "",
            node=run_row.node if run_row is not None else "",
            attempt=run_row.attempt if run_row is not None else 0,
            turns=run_row.turns if run_row is not None else 0,
            cost_usd=run_row.cost_usd if run_row is not None else 0.0,
            phases_landed=landed,
            phases_total=total,
        ))
    return rows


def gather(
    runs_dir: Path, work_dir: Path, now: str, local_name: str, local_capacity: int, spend: dict,
) -> dict[str, list]:
    """Edge. One call to each reader (`work_items` through `_work_items_snapshot`, so it runs at most once per
    `_PHASE_SNAPSHOT_S` window); no other I/O beyond that. `work_dir` is the workspace; drafts live in its
    `work` directory. `local_name`/`local_capacity` seat the local machine's own hosts row and its lanes-in-use
    count. `spend` is a plain dict the caller already computed (the CLI edge, from the same sources the chair
    loop uses); this module never reads usage, pacing or run-store cost to build it."""
    chair_state = runs_top_screen.chair_now(runs_dir)
    actions = chair_read_stale.read_chair_actions(runs_dir)
    end = _parse_iso(now)
    recent = [row for row in actions if end is not None and _is_recent_needs_chair(row, end)]
    lanes = run_store.live_lanes(runs_dir, now)
    hosts = with_local_host(run_store.hosts(runs_dir), local_name, local_capacity)
    run_rows = {row.run: row for row in runs_top_screen.rows_now(runs_dir)}
    items = _work_items_snapshot(str(runs_dir), int(time.monotonic() // _PHASE_SNAPSHOT_S))
    return {
        "drafts": draft_list.read_drafts(work_dir / "work", now),
        "hosts": _hosts_with_lane_counts(hosts, lanes, local_name),
        "lanes": _lane_rows(lanes, run_rows, items),
        "chair": [chair_state] if chair_state is not None else [],
        "spend": spend,
        "needs_chair": newest_per_item(recent),
    }


def _age(seconds: float) -> str:
    """`Ns`/`Nm`/`Nh`/`Nd` for a non-negative age in seconds, coarsest unit that is at least 1."""
    whole = max(int(seconds), 0)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    if whole < 86400:
        return f"{whole // 3600}h"
    return f"{whole // 86400}d"


def _clock_and_age(ts: object, now: datetime, tz: tzinfo) -> str:
    """`ts` (a stored UTC timestamp) as local clock time plus age against `now`, e.g. `8:50 AM  2m ago`;
    `?` when `ts` does not parse as a timestamp."""
    parsed = _parse_iso(ts)
    if parsed is None:
        return "?"
    clock = parsed.astimezone(tz).strftime("%I:%M %p").lstrip("0")
    return f"{clock}  {_age((now - parsed).total_seconds())} ago"


def _draft_line(row, now: datetime, tz: tzinfo) -> str:
    return f"{row.id}  {row.proposed_by}  {draft_list.format_age(row.age_seconds)}"


def _host_line(row: dict, now: datetime, tz: tzinfo) -> str:
    versions = row.get("versions_json")
    if isinstance(versions, str):
        try:
            versions = json.loads(versions)
        except ValueError:
            versions = {}
    login = {True: "login ok", False: "login lapsed"}.get((versions or {}).get("login_ok"), "login ?")
    beat = row.get("beat_at")
    beat_text = _clock_and_age(beat, now, tz) if beat else "never"
    in_use = row.get("in_use", 0)
    return f"{row.get('name')}  {in_use}/{row.get('capacity')}  {row.get('state')}  beat={beat_text}  {login}"


def _lane_line(row, now: datetime, tz: tzinfo) -> str:
    beat = f"beat={_clock_and_age(row.heartbeat_at, now, tz)}"
    run_cols = f"{row.phase}  {row.node}  att {row.attempt}  turns {row.turns}  ${row.cost_usd:.2f}"
    progress = f"{row.phases_landed}/{row.phases_total} phases"
    return f"{row.run}  {row.host or '-'}  {beat}  {run_cols}  {progress}"


def _chair_line(row: dict, now: datetime, tz: tzinfo) -> str:
    return f"{row.get('holder')}  ({row.get('state')}, beat {row.get('minutes_ago')}m ago)"


def _needs_chair_line(row: dict, now: datetime, tz: tzinfo) -> str:
    initiative, cause = _needs_chair_key(row)
    return f"{initiative}  {cause}  {_clock_and_age(row.get('ts'), now, tz)}"


_ITEM_LINE = {
    "drafts": _draft_line,
    "hosts": _host_line,
    "lanes": _lane_line,
    "chair": _chair_line,
    "needs_chair": _needs_chair_line,
}


def _fraction_text(label: str, fraction: float | None) -> str:
    return f"{label} n/a" if fraction is None else f"{label} {fraction:.0%}"


def _five_hour_text(fraction: float | None) -> str:
    return _fraction_text("5h", fraction)


def _weekly_text(fraction: float | None, hard_stop_fraction: float) -> str:
    if fraction is None:
        return "weekly n/a"
    return f"weekly {fraction:.0%}/{hard_stop_fraction:.0%}"


def _spend_header(spend: dict) -> str:
    return f"spend: {_five_hour_text(spend.get('five_hour'))}  {_weekly_text(spend.get('weekly'), spend.get('hard_stop', 0.0))}"


def render(sections: dict, selected: int, width: int, now: datetime, tz: tzinfo) -> list[str]:
    """Pure. One spend header line first, when `sections["spend"]` carries a dict, then a header per section and
    one line per item; `>` marks the selected item among the selectable sections (drafts, hosts, lanes, chair, in
    that order); `needs_chair` rows are shown but never selectable. Every line is cut to `width`; timestamps show
    as clock time in `tz` plus age against `now`."""
    lines: list[str] = []
    spend = sections.get("spend")
    if spend:
        lines.append(_spend_header(spend)[:width])
    index = 0
    for name in _SECTION_ORDER:
        lines.append(f"{name.replace('_', ' ')}:"[:width])
        selectable = name in _SELECTABLE_SECTIONS
        for row in sections.get(name, []):
            marker = "> " if selectable and index == selected else "  "
            lines.append((marker + _ITEM_LINE[name](row, now, tz))[:width])
            if selectable:
                index += 1
    return lines


def _selection(name: str, row) -> dict:
    if name == "drafts":
        return {"kind": "draft", "id": row.id}
    if name == "hosts":
        return {"kind": "host", "name": row["name"]}
    if name == "lanes":
        return {"kind": "lane", "run": row.run, "pid": None}
    return {"kind": "chair"}


def _selectable_rows(sections: dict) -> list[tuple[str, object]]:
    """Pure. Every selectable row paired with its section name, in selection order: drafts, hosts, lanes, chair."""
    return [(name, row) for name in _SELECTABLE_SECTIONS for row in sections.get(name, [])]


def selection_at(sections: dict, index: int) -> dict | None:
    """Pure. The `plan_command` selection for the index-th selectable item; None past the last one."""
    flat = _selectable_rows(sections)
    return _selection(*flat[index]) if 0 <= index < len(flat) else None


def _last_line(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _draw(stdscr, sections: dict, selected: int, message: str | None, now: datetime, tz: tzinfo) -> None:
    import curses

    stdscr.clear()
    height, width = stdscr.getmaxyx()
    lines = render(sections, selected, width, now, tz)
    if message is not None:
        lines = [*lines, message[:width]]
    for row_i, line in enumerate(lines[:height]):
        with contextlib.suppress(curses.error):
            stdscr.addnstr(row_i, 0, line, width)
    stdscr.refresh()


def _act(stdscr, sections: dict, selected: int, key: str) -> str | None:
    """Edge. Plans, confirms and only on `y` runs the command for `key` on the selected row; None with no command,
    no selection, or a non-`y` answer."""
    import curses

    selection = selection_at(sections, selected)
    if selection is None:
        return None
    argv = console_plan.plan_command(selection, key)
    if argv is None:
        return None
    height, width = stdscr.getmaxyx()
    with contextlib.suppress(curses.error):
        stdscr.addnstr(height - 1, 0, console_plan.confirm_line(argv)[:width], width)
    stdscr.refresh()
    confirm = stdscr.getch()
    if confirm not in (ord("y"), ord("Y")):
        return None
    result = subprocess.run(argv, capture_output=True, text=True)
    return _last_line(result.stdout) or _last_line(result.stderr)


def loop(
    stdscr, runs_dir: Path, work_dir: Path, local_name: str, local_capacity: int,
    spend_fn: Callable[[], dict], interval: float,
) -> int:
    import curses

    with contextlib.suppress(curses.error):  # no real terminal behind stdscr, e.g. under test
        curses.curs_set(0)
    stdscr.timeout(int(interval * 1000))
    selected = 0
    message: str | None = None
    while True:
        now = datetime.now(UTC)
        tz = now.astimezone().tzinfo
        sections = gather(runs_dir, work_dir, now.isoformat(), local_name, local_capacity, spend_fn())
        total = len(_selectable_rows(sections))
        selected = min(selected, total - 1) if total else 0
        _draw(stdscr, sections, selected, message, now, tz)
        message = None
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            return 0
        if ch == curses.KEY_DOWN:
            selected = min(selected + 1, total - 1) if total else 0
        elif ch == curses.KEY_UP:
            selected = max(selected - 1, 0)
        elif 0 <= ch < 256:
            message = _act(stdscr, sections, selected, chr(ch))
        # curses.KEY_RESIZE and a plain timeout both fall through here: the
        # next iteration redraws against the current sections and size.


def main(
    runs_dir: Path, work_dir: Path, local_name: str, local_capacity: int,
    spend_fn: Callable[[], dict], interval: float = 5.0,
) -> int:
    import curses
    import signal

    def _hangup(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGHUP, _hangup)
    try:
        return curses.wrapper(
            lambda stdscr: loop(stdscr, runs_dir, work_dir, local_name, local_capacity, spend_fn, interval)
        )
    except KeyboardInterrupt:
        # A closed window sends SIGHUP; raising through the wrapper lets it
        # restore the terminal before this function returns.
        return 0
    finally:
        signal.signal(signal.SIGHUP, previous)
