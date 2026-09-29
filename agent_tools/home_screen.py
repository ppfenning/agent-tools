"""Curses edge for `cox home`: gathers each panel from the tool behind it,
each on its own timeout, hands the result to `home_model`'s pure frame, draws
it, and runs the effect a keypress's `home_model.step` returns. `curses` is
imported inside each function that needs it, so this module imports on a
machine with no terminal and the gatherers stay testable with a fake stdscr.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import itertools
import json
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import (
    home_layout,
    home_model,
    leader,
    leader_chat,
    route,
    runs_top_screen,
    theme,
    usage_meter,
    usage_window,
)
from agent_tools.home_model import Span
from agent_tools.pacing import assess

__all__ = ["draw", "facts", "load_layout", "main", "run_effect", "save_layout"]

_TIMEOUT_SECONDS = 2.0
_REFRESH_SECONDS = 2.0
_MARK = "! "


def _read_with_timeout(reader, timeout_seconds: float):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(reader)
    try:
        return future.result(timeout=timeout_seconds)
    except Exception:
        return None
    finally:
        executor.shutdown(wait=False)  # a genuinely hung reader's thread is abandoned, never joined


def _panel(cache: dict, name: str, reader, timeout_seconds: float, now: float):
    value = _read_with_timeout(reader, timeout_seconds)
    if value is not None:
        return value, "fresh", {**cache, name: (value, now)}
    cached_value, cached_at = cache.get(name, (None, None))
    age = None if cached_at is None else now - cached_at
    return cached_value, home_model.panel_status(cached_value, age, timeout_seconds), cache


def _read_leader(runs_dir) -> dict:
    record = leader.read(runs_dir)
    return record if record is not None else {}


def _read_chat(runs_dir, limit: int = 20) -> tuple[dict, ...]:
    path = leader_chat.chat_path(runs_dir)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return tuple(leader_chat.read_thread(text, limit))


def _key_for(ch: int) -> str:
    """`ENTER`/`ESC` by name for `home_model.step`; any other byte as its character, else empty."""
    import curses

    if ch in (10, 13, curses.KEY_ENTER):
        return "ENTER"
    if ch == 27:
        return "ESC"
    return chr(ch) if 0 <= ch < 256 else ""


def _send_chat(runs_dir, text: str) -> None:
    path = leader_chat.chat_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    entry = {"at": datetime.now(UTC).isoformat(), "from": "operator", "text": text}
    path.write_text(leader_chat.append_line(existing, entry), encoding="utf-8")


def _read_window(runs_dir, now_dt: datetime, window_ceiling_usd: float | None = None) -> dict:
    estimate_window = usage_window.gather(runs_dir, now_dt, ceiling_usd=window_ceiling_usd)
    meter = usage_meter.read()
    meter_window = (
        usage_meter.as_window(meter.five_hour, now_dt, timedelta(hours=5))
        if meter is not None and usage_meter.fresh(meter, now_dt)
        else None
    )
    window = usage_meter.prefer(meter_window, estimate_window)
    result = assess(window, usage_window.DEFAULT_POLICY, now_dt)
    return {
        "tier": result.tier_ceiling,
        "effort_ceiling": result.effort_ceiling,
        "spent_usd": window.spent_usd,
        "time_to_reset": home_model.time_to_reset(window.end, now_dt),
        "reason": result.reason,
        "block_left": usage_window.block_remaining(window, now_dt)[1],
        "ceiling_left": usage_window.ceiling_remaining(window),
        "ceiling_usd": window.ceiling_usd,
        "source": "meter" if meter_window is not None else "estimate",
    }


def _backlog(work_dir, intake_dir) -> dict:
    work_dir, intake_dir = Path(work_dir), Path(intake_dir)
    items = [
        route.work_item(route.parse_frontmatter(path.read_text(encoding="utf-8"))[0],
                         initiative=path.parent.parent.name, phase_dir=path.parent.name, stem=path.stem)
        for path in sorted(work_dir.glob("*/*/*.md")) if path.name != "initiative.md"
    ]
    texts = {p.name: (p / "initiative.md").read_text(encoding="utf-8")
             for p in sorted(work_dir.glob("*")) if (p / "initiative.md").is_file()}
    states = route.initiative_states(sorted(texts), items)
    initiatives = [{"id": iid, "done": states[iid], "text": text} for iid, text in texts.items()]
    files = {
        str(path.relative_to(intake_dir)): path.read_text(encoding="utf-8")
        for path in sorted(intake_dir.glob("*.md")) + sorted((intake_dir / "done").glob("*.md"))
    }
    groups = route.intake_groups(route.intake_entries(files), initiatives)
    ready = {summary["id"]: summary["ready"] for summary in route.initiative_summaries(items)}
    return {"queued": len(groups["queued"]), "decomposed": len(groups["decomposed"]),
            "landed": len(groups["landed"]), "ready": ready}


def facts(runs_dir, work_dir, intake_dir, now: float, cache: dict | None = None,
          timeout_seconds: float = _TIMEOUT_SECONDS,
          window_ceiling_usd: float | None = None) -> tuple[home_model.Facts, dict]:
    """One `home_model.Facts` and the cache the next call should pass back in."""
    cache = dict(cache or {})
    now_dt = datetime.fromtimestamp(now, tz=UTC)

    leader_value, leader_status, cache = _panel(cache, "leader", lambda: _read_leader(runs_dir), timeout_seconds, now)
    runs_value, runs_status, cache = _panel(
        cache, "runs", lambda: tuple(runs_top_screen.rows_now(runs_dir)), timeout_seconds, now)
    backlog_value, backlog_status, cache = _panel(
        cache, "backlog", lambda: _backlog(work_dir, intake_dir), timeout_seconds, now)
    window_value, window_status, cache = _panel(
        cache, "window", lambda: _read_window(runs_dir, now_dt, window_ceiling_usd), timeout_seconds, now)
    chat_value, chat_status, cache = _panel(cache, "chat", lambda: _read_chat(runs_dir), timeout_seconds, now)

    leader_record = leader_value or None
    alive = leader.pid_alive(leader_record["pid"]) if leader_record and isinstance(leader_record.get("pid"), int) else False
    result = home_model.Facts(
        leader=leader_record,
        leader_liveness=leader.liveness(leader_record, alive, now_dt, socket.gethostname()),
        runs_rows=runs_value or (),
        backlog=backlog_value or {},
        window=window_value or {},
        now=now,
        chat=chat_value or (),
    )
    cache["_status"] = {
        "leader": leader_status, "runs": runs_status, "backlog": backlog_status,
        "window": window_status, "chat": chat_status,
    }
    return result, cache


_PANEL_STATUS_KEYS = (("Leader", "leader"), ("Backlog", "backlog"), ("Window", "window"), ("Runs", "runs"))


def _marked(line: home_model.Line, stale_titles: set[str]) -> home_model.Line:
    """`line` with `_MARK` prefixed to every span whose role is `"title"` and text names a stale panel."""
    return tuple(
        dataclasses.replace(span, text=f"{_MARK}{span.text}") if span.role == "title" and span.text in stale_titles
        else span
        for span in line
    )


def _paint(stdscr, y: int, line: home_model.Line, width: int, attrs: dict[str, int]) -> None:
    """`attrs` maps a role to a curses attribute, already resolved via `curses.color_pair`."""
    import curses

    col = 0
    for span in line:
        if col >= width:
            break
        with contextlib.suppress(curses.error):
            stdscr.addnstr(y, col, span.text, width - col, attrs.get(span.role, attrs.get("plain", 0)))
        col += len(span.text)


def _layout_path() -> Path:
    return Path.home() / ".config" / "cox" / "home.json"


def load_layout(path: Path) -> home_layout.Layout:
    """`DEFAULT` when the file is missing, unreadable or malformed, so `cox home` always opens."""
    try:
        return home_layout.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return home_layout.DEFAULT


def save_layout(path: Path, layout: home_layout.Layout) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(home_layout.to_json(layout)), encoding="utf-8")


def draw(stdscr, facts_obj: home_model.Facts, state: home_model.State, statuses: dict,
         attrs: dict[str, int], layout: home_layout.Layout | None = None) -> None:
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    chat_lines = home_model.chat_pane(facts_obj.chat, width, state.chat_draft)
    body_height = max(height - len(chat_lines), 0)
    frame_lines = (home_model.frame(facts_obj, state, width, body_height) if layout is None
                   else home_layout.render(layout, facts_obj, width, body_height))
    stale_titles = {title for title, key in _PANEL_STATUS_KEYS if statuses.get(key, "fresh") != "fresh"}
    lines = [_marked(line, stale_titles) for line in frame_lines] + [(Span(text),) for text in chat_lines]
    for i, line in enumerate(lines[:height]):
        _paint(stdscr, i, line, width, attrs)
    stdscr.refresh()


def run_effect(effect, runner=subprocess.run) -> bool:
    """Runs `effect`'s subprocess against a suspended curses screen; returns whether the loop should stop."""
    import curses

    if isinstance(effect, home_model.Quit):
        return True
    if isinstance(effect, home_model.Refuse):
        return False
    if isinstance(effect, home_model.Talk):
        plugin_flag = ["--plugin-dir", effect.plugin_dir] if effect.plugin_dir else []
        argv = ["claude", *plugin_flag, effect.opening]
    elif isinstance(effect, home_model.Setup):
        argv = list(effect.argv)
    else:
        return False
    with contextlib.suppress(curses.error):  # no real terminal behind stdscr, e.g. under test
        curses.endwin()
    try:
        runner(argv)
    finally:
        with contextlib.suppress(curses.error):
            curses.doupdate()
    return False


def main(runs_dir, work_dir, intake_dir, plugin_dir: str, refresh_seconds: float = _REFRESH_SECONDS,
         window_ceiling_usd: float | None = None) -> int:
    import curses

    def _loop(stdscr):
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        stdscr.timeout(int(refresh_seconds * 1000))
        numbers = theme.install(theme.resolve("default"))
        attrs = {role: curses.color_pair(n) for role, n in numbers.items()}
        cache: dict = {}
        state = home_model.State(plugin_dir=plugin_dir, leader_liveness="none", other_holder=None)
        ticks = itertools.count()
        path = _layout_path()
        layout, focus = load_layout(path), "runs"
        while True:
            facts_obj, cache = facts(runs_dir, work_dir, intake_dir, time.time(), cache,
                                      window_ceiling_usd=window_ceiling_usd)
            facts_obj = dataclasses.replace(facts_obj, tick=next(ticks))
            other_holder = facts_obj.chair.get("session") if facts_obj.chair and facts_obj.chair_liveness == "live" else None
            state = dataclasses.replace(state, leader_liveness=facts_obj.chair_liveness, other_holder=other_holder)
            draw(stdscr, facts_obj, state, cache.get("_status", {}), attrs, layout)
            ch = stdscr.getch()
            if ch == -1:
                continue
            key = _key_for(ch)
            if key in home_layout.KEYS and not state.chat_focused:
                focus = home_layout.focus_after(key, focus)
                stepped = home_layout.step(layout, key, focus)
                if stepped != layout:
                    save_layout(path, stepped)
                layout = stepped
                continue
            state, effect = home_model.step(state, key)
            if isinstance(effect, home_model.Send):
                _send_chat(runs_dir, effect.text)
                continue
            if effect is not None and run_effect(effect):
                return 0

    return curses.wrapper(_loop)
