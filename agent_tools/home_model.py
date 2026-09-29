"""Pure core for `cox home`: Facts in, panes and step's effect out. No curses, subprocess, or file reads."""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    text: str
    role: str = "plain"


Line = tuple[Span, ...]

from agent_tools import panel, regatta, runs_top  # noqa: E402  (after Span/Line so panel.py can import them back)

__all__ = [
    "Drill",
    "Effect",
    "Facts",
    "Intake",
    "Land",
    "Line",
    "Quit",
    "Refuse",
    "Send",
    "Setup",
    "Span",
    "State",
    "Talk",
    "attention_pane",
    "backlog_pane",
    "chair_pane",
    "chat_pane",
    "frame",
    "health_pane",
    "layout",
    "leader_pane",
    "panel_status",
    "runs_pane",
    "step",
    "time_to_reset",
    "window_pane",
]

_ATTENTION_MARK = "! "
_ELLIPSIS = "…"
_SIDE_BY_SIDE_WIDTH = 160
_COLUMN_COUNT = 3
_REGATTA_MIN_HEIGHT = 24
_REGATTA_MIN_PANEL = 3
OPENING_CONTEXT = "cox route context"
SETUP_ARGV = ("cox", "setup")
_ATTENTION_REASONS = {"exited": ("gate", "l"), "quarantined": ("quarantine", "i"), "budget": ("budget stop", "i")}


@dataclass(frozen=True)
class Facts:
    leader: dict | None
    leader_liveness: str
    runs_rows: tuple[runs_top.Row, ...]
    backlog: dict
    window: dict
    now: float
    chat: tuple[dict, ...] = ()
    tick: int = 0

    @property
    def chair(self) -> dict | None:
        return self.leader

    @property
    def chair_liveness(self) -> str:
        return self.leader_liveness


@dataclass(frozen=True)
class State:
    plugin_dir: str
    leader_liveness: str
    other_holder: str | None
    selected_run: str | None = None
    selected_status: str | None = None
    land_armed: str | None = None
    chat_focused: bool = False
    chat_draft: str = ""

    @property
    def chair_liveness(self) -> str:
        return self.leader_liveness


@dataclass(frozen=True)
class Talk:
    plugin_dir: str
    opening: str = OPENING_CONTEXT


@dataclass(frozen=True)
class Setup:
    argv: tuple[str, ...] = SETUP_ARGV


@dataclass(frozen=True)
class Quit:
    pass


@dataclass(frozen=True)
class Refuse:
    holder: str


@dataclass(frozen=True)
class Drill:
    run: str


@dataclass(frozen=True)
class Land:
    run: str
    apply: bool


@dataclass(frozen=True)
class Intake:
    pass


@dataclass(frozen=True)
class Send:
    text: str


Effect = Talk | Setup | Quit | Refuse | Drill | Land | Intake | Send


def _refused(state: State) -> bool:
    return state.chair_liveness == "live" and bool(state.other_holder)


def step(state: State, key: str) -> tuple[State, Effect | None]:
    """Only 'l' refuses under a foreign live leader, being the one key that writes; it arms an exited run on its first press, applies on a consecutive press on that same run, and disarms the moment the selection differs from what is armed. While chat holds focus every other key, including the action letters, extends the draft instead of firing."""
    if state.land_armed is not None and state.land_armed != state.selected_run:
        state = dataclasses.replace(state, land_armed=None)
    if state.chat_focused:
        if key == "ESC":
            return dataclasses.replace(state, chat_focused=False), None
        if key == "ENTER":
            if not state.chat_draft:
                return state, None
            return dataclasses.replace(state, chat_draft=""), Send(state.chat_draft)
        if len(key) == 1 and key.isprintable():
            return dataclasses.replace(state, chat_draft=state.chat_draft + key), None
        return state, None
    if key == "c":
        return dataclasses.replace(state, chat_focused=True), None
    if key == "t":
        return state, Talk(state.plugin_dir)
    if key == "s":
        return state, Setup()
    if key == "q":
        return state, Quit()
    if key == "ENTER":
        if state.selected_run is None:
            return state, None
        return state, Drill(state.selected_run)
    if key == "l":
        if _refused(state):
            return state, Refuse(state.other_holder)
        if state.selected_run is None or state.selected_status != "exited":
            return state, None
        apply = state.selected_run == state.land_armed
        armed = None if apply else state.selected_run
        return dataclasses.replace(state, land_armed=armed), Land(state.selected_run, apply)
    if key == "i":
        return state, Intake()
    return state, None


def _cut(line: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(line) <= width:
        return line
    return line[: max(width - 1, 0)] + _ELLIPSIS


def _heartbeat_age(chair: dict | None, now: float) -> float | None:
    """Seconds between `now` and the chair's own `heartbeat_at`, or None with no chair or no parseable timestamp."""
    if chair is None:
        return None
    try:
        heartbeat_at = datetime.datetime.fromisoformat(chair["heartbeat_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return now - heartbeat_at.timestamp()


def panel_status(last_value, age_seconds: float | None, timeout_seconds: float) -> str:
    """fresh within `timeout_seconds`, stale past it with a value, absent with none."""
    if last_value is None:
        return "absent"
    if age_seconds is None or age_seconds > timeout_seconds:
        return "stale"
    return "fresh"


def _cut_span(text: str, width: int, role: str = "plain") -> Line:
    return (Span(_cut(text, width), role),)


def chair_pane(facts: Facts, width: int) -> tuple[Line, ...]:
    holder = (facts.chair or {}).get("session", "none")
    live_runs = any(r.alive for r in facts.runs_rows)
    attention = facts.chair_liveness in ("none", "stale", "crashed") and live_runs
    mark = _ATTENTION_MARK if attention else ""
    age = _heartbeat_age(facts.chair, facts.now)
    heartbeat = f"{age:.0f}s ago" if age is not None else "n/a"
    role = "alert" if attention else "ok" if facts.chair_liveness == "live" else "plain"
    text = f"{mark}holder: {holder}  status: {facts.chair_liveness}  heartbeat: {heartbeat}"
    return (_cut_span(text, width, role),)


# Back-compat alias: home_screen.py still calls `leader_pane` until its own rename ticket lands.
leader_pane = chair_pane


def runs_pane(facts: Facts, width: int) -> tuple[Line, ...]:
    return tuple((Span(line),) for line in runs_top.render(list(facts.runs_rows), width))


def attention_pane(facts: Facts, width: int) -> tuple[str, ...]:
    lines = tuple(
        f"{row.run}: {_ATTENTION_REASONS[row.status][0]} [{_ATTENTION_REASONS[row.status][1]}]"
        for row in facts.runs_rows
        if row.status in _ATTENTION_REASONS
    )
    return tuple(_cut(line, width) for line in lines)


def backlog_pane(facts: Facts, width: int) -> tuple[Line, ...]:
    b = facts.backlog
    counts = f"queued {b.get('queued', 0)}  decomposed {b.get('decomposed', 0)}  landed {b.get('landed', 0)}"
    ready = b.get("ready", {})
    ready_line = "ready: " + (", ".join(f"{k}={v}" for k, v in ready.items()) or "none")
    return tuple(_cut_span(line, width) for line in (counts, ready_line))


def time_to_reset(end: datetime.datetime, now: datetime.datetime) -> str:
    return f"{max(int((end - now).total_seconds() // 60), 0)}m"


def _spend_line(w: Mapping) -> str:
    """A `source` of "meter" carries plan-limit percent in `spent_usd`, not dollars."""
    ceiling_usd = w.get("ceiling_usd")
    if w.get("source") == "meter":
        return f"used {w.get('spent_usd', 0):.0f}% of plan limit  {w.get('ceiling_left', 0):.0%} left"
    if ceiling_usd:
        return f"spent ${w.get('spent_usd', 0):.2f} of ${ceiling_usd:.0f} ceiling  {w.get('ceiling_left', 0):.0%} left"
    return f"spent ${w.get('spent_usd', 0):.2f}  no ceiling set"


def window_pane(facts: Facts, width: int) -> tuple[Line, ...]:
    w = facts.window
    verdict = f"tier {w.get('tier', '')} effort {w.get('effort_ceiling', '')}"
    block = f"block {w.get('block_left', 0):.0%} left  resets in {w.get('time_to_reset', '')}"
    spend = _spend_line(w)
    reason = w.get("reason", "")
    lines = (verdict, block, spend, reason) if reason else (verdict, block, spend)
    return tuple(_cut_span(line, width) for line in lines)


def health_pane(rows: list[dict], width: int) -> tuple[str, ...]:
    failing = tuple(f"{row['check']}: {row['detail']}" for row in rows if not row["ok"])
    lines = failing or ("health: ok",)
    return tuple(_cut(line, width) for line in lines)


def chat_pane(thread: Sequence[Mapping], width: int, draft: str) -> tuple[str, ...]:
    lines = ("CHAT", *(f"{e.get('from', '?')}: {e.get('text', '')}" for e in list(thread)[-3:]), f"> {draft}")
    return tuple(_cut(line, width) for line in lines)


def _columns(width: int, n: int) -> tuple[int, ...]:
    """`n` column widths whose sum plus `n - 1` single-space separators is exactly `width`."""
    base, extra = divmod(width - (n - 1), n)
    return tuple(base + (1 if i < extra else 0) for i in range(n))


def _join_boxes(boxes: tuple[tuple[Line, ...], ...]) -> tuple[Line, ...]:
    sep = (Span(" "),)
    return tuple(sum(((sep + part if i else part) for i, part in enumerate(row)), ()) for row in zip(*boxes))


def _regatta_height(runs: int, spare: int, height: int) -> int:
    """Rows for the regatta: one per run plus finish and border, trimmed to `spare`, none under either minimum."""
    fitted = min(runs + 3, spare)
    return fitted if height >= _REGATTA_MIN_HEIGHT and fitted >= _REGATTA_MIN_PANEL else 0


def frame(facts: Facts, state: State, width: int, height: int) -> tuple[Line, ...]:
    """Layout: REGATTA on top when the runs table still fits beneath it; Leader/Backlog/Window boxed equal width
    and height, side by side at or above `_SIDE_BY_SIDE_WIDTH` else stacked; `runs_pane` boxed full width into
    whatever of `height` remains."""
    side_by_side = width >= _SIDE_BY_SIDE_WIDTH
    widths = _columns(width, _COLUMN_COUNT) if side_by_side else (width, width, width)
    titles = ("Leader", "Backlog", "Window")
    panes = tuple(pane(facts, w - 2) for pane, w in zip((chair_pane, backlog_pane, window_pane), widths))
    box_height = min(max(len(p) for p in panes) + 2, height)
    boxes = tuple(panel.box(title, body, w, box_height) for title, body, w in zip(titles, panes, widths))
    top = _join_boxes(boxes) if side_by_side else tuple(line for b in boxes for line in b)
    top_height = box_height if side_by_side else box_height * len(boxes)
    runs_body = runs_pane(facts, width - 2)
    sail_height = _regatta_height(len(facts.runs_rows), height - top_height - len(runs_body) - 2, height)
    sail = panel.box("REGATTA", regatta.regatta(facts.runs_rows, width - 2, facts.tick), width, sail_height)
    runs = panel.box("Runs", runs_body, width, max(height - top_height - sail_height, 0))
    return (*sail, *top, *runs)


def _paired(names: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """`names` two to a row, a trailing odd one alone."""
    return tuple(names[i : i + 2] for i in range(0, len(names), 2))


def layout(width: int, height: int, panels: Mapping[str, tuple[str, ...]]) -> tuple[tuple[str, int], ...]:
    """`(name, height)` per panel in `panels`' own order: `\"runs\"` always its own full-width row and last;
    the rest one per row under `_SIDE_BY_SIDE_WIDTH`, else paired two to a row; each row's height capped to
    what remains of `height`, and a name prefixed with `_ATTENTION_MARK` where that cut it short."""
    others = tuple(name for name in panels if name != "runs")
    grouped = tuple((name,) for name in others) if width < _SIDE_BY_SIDE_WIDTH else _paired(others)
    rows = grouped + ((("runs",),) if "runs" in panels else ())
    remaining = height
    result: dict[str, tuple[str, int]] = {}
    for row in rows:
        natural = max(len(panels[name]) for name in row)
        allotted = min(natural, remaining)
        remaining -= allotted
        for name in row:
            shown = min(len(panels[name]), allotted)
            cut = shown < len(panels[name])
            result[name] = (f"{_ATTENTION_MARK}{name}" if cut else name, shown)
    return tuple(result[name] for name in panels)
