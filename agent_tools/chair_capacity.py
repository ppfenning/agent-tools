"""Edge: the chair loop's lane cap, read the one way for every caller.

`cox chair run`, `cox console` and the `cox dash` feed all ask `chair_max_in_flight`, so the feed shows the
capacity the loop enforces. The wrong belief was that the pacing policy's `max_in_flight` is that cap: the
team cartridge's `policy.dispatch.max_in_flight` wins over it, and the pacing file is only a fallback.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from agent_tools import chair_cap

# The graphs dispatch loop's default (`_DEFAULT_MAX_IN_FLIGHT` in harness/cos.py), used when no cartridge or policy file names a cap.
DEFAULT_MAX_IN_FLIGHT = 3


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _first_along_extends[T](cartridges_dir: Path, team: str, read: Callable[[str | None], T | None]) -> T | None:
    """The first non-None `read` along the team cartridge and its `extends` chain, all read from `cartridges_dir`."""
    pending, seen = [team], set()
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        text = _read_text_or_none(cartridges_dir / name / "cartridge.yaml")
        value = read(text)
        if value is not None:
            return value
        pending.extend(chair_cap.extends_of(text))
    return None


def _cartridge_cap(cartridges_dir: Path, team: str) -> int | None:
    """The first `policy.dispatch.max_in_flight` along the team cartridge and its `extends` chain."""
    return _first_along_extends(cartridges_dir, team, chair_cap.cap_from_cartridge)


def chair_local_lanes(profile: dict) -> str:
    """`policy.dispatch.local_lanes` along the team cartridge and its `extends` chain, else "any"."""
    cartridges_dir, team = profile.get("cartridges_dir"), profile.get("team")
    if not (cartridges_dir and isinstance(team, str) and team):
        return "any"
    found = _first_along_extends(Path(cartridges_dir).expanduser(), team, chair_cap.local_lanes_from_cartridge)
    return found if found is not None else "any"


def chair_max_in_flight(runs_dir: Path, profile: dict) -> int:
    """The lane cap: the team cartridge's `policy.dispatch.max_in_flight`, else its `extends` chain's, else `<runs_dir>/policy.pacing.json`'s, else 3."""
    cartridges_dir, team = profile.get("cartridges_dir"), profile.get("team")
    from_cartridge = _cartridge_cap(Path(cartridges_dir).expanduser(), team) if cartridges_dir and isinstance(team, str) and team else None
    if from_cartridge is not None:
        return from_cartridge
    text = _read_text_or_none(Path(runs_dir) / "policy.pacing.json")
    try:
        raw = json.loads(text) if text is not None else {}
    except json.JSONDecodeError:
        raw = {}
    value = raw.get("max_in_flight") if isinstance(raw, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else DEFAULT_MAX_IN_FLIGHT
