"""Notifier dispatcher: the thin edge joining the pure `due` and `render` to `post`, with the sent map kept in a state file."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from agent_tools import notify_ntfy
from agent_tools.notify_core import Event, NotifyConfig, due, render


def _read_state(path: Path) -> dict[str, float]:
    """A missing, empty or corrupt file, or JSON that is not an object, reads as an empty map."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: float(t) for k, t in raw.items() if isinstance(t, (int, float)) and not isinstance(t, bool)}


def _write_state(path: Path, state: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _send(post: Callable[..., bool], url: str, event: Event) -> bool:
    """A raising `post` counts as a failed push, so earlier pushes in the same call are still recorded."""
    try:
        return post(url, *render(event)) is True
    except Exception:  # a dead phone channel must never stop the caller
        return False


def dispatch(events: Sequence[Event], config: NotifyConfig | None, state_path: Path, now: float,
             post: Callable[..., bool] = notify_ntfy.post) -> list[str]:
    """Keys pushed, in event order. `config` None is the off state: no file access, no push.
    A key is recorded only when `post` returned True, so a failed push is tried again on the next call."""
    if config is None:
        return []
    sent = _read_state(state_path)
    fresh, recorded = due(events, sent, now)
    pushed = [e.key for e in fresh if _send(post, config.ntfy_url, e)]
    # `due` stamps every fresh key with `now`, an expired one already in `sent` included; drop the unsent ones.
    failed = {e.key for e in fresh} - set(pushed)
    new = {k: t for k, t in recorded.items() if k not in failed}
    if new != sent:
        _write_state(state_path, new)
    return pushed
