"""Pure notifier core: event type, message rendering, rate-limit decision, profile config.

Profile shape: `notify: {ntfy: https://ntfy.sh/pat-coxswain}`. A missing or empty `ntfy` means the notifier is off.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

KINDS = ("needs_chair", "host_login", "stall", "release_cut", "decision_card")

TITLE_MAX = 60
BODY_MAX = 200


@dataclass(frozen=True)
class Event:
    kind: str
    key: str
    title: str
    body: str
    click: str = ""


@dataclass(frozen=True)
class NotifyConfig:
    ntfy_url: str


def config_from_profile(profile: Mapping) -> NotifyConfig | None:
    notify = profile.get("notify")
    url = notify.get("ntfy") if isinstance(notify, Mapping) else None
    return NotifyConfig(url) if isinstance(url, str) and url else None


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(event: Event) -> tuple[str, str, str]:
    """Title is the first line, at most 60 characters; body at most 200, ellipsis included."""
    lines = event.title.splitlines()
    title = (lines[0] if lines else "")[:TITLE_MAX]
    return title, _cut(event.body, BODY_MAX), event.click


def due(events: Sequence[Event], sent: Mapping[str, float], now: float,
        window_s: float = 3600) -> tuple[list[Event], dict[str, float]]:
    """Events to push and the new sent map. A key pushed less than `window_s` ago is dropped; older entries are pruned."""
    live = {k: t for k, t in sent.items() if now - t < window_s}
    fresh = [e for i, e in enumerate(events) if e.key not in live and e.key not in {p.key for p in events[:i]}]
    return fresh, {**live, **{e.key: now for e in fresh}}
