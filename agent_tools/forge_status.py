"""Forge status reader: parse the GitHub status summary and cache it for five minutes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

CACHE_SECONDS = 300


@dataclass(frozen=True)
class ForgeStatus:
    degraded: bool
    incident: str  # empty when none
    fetched_at: float  # epoch seconds


def _watched(name: str) -> bool:
    return name == "Actions" or "CI" in name


def parse_github_summary(payload: dict, fetched_at: float = 0.0) -> ForgeStatus:
    """Degraded when Actions or a CI component is not operational; the summary lists only unresolved incidents."""
    down = [
        str(c.get("name", ""))
        for c in payload.get("components") or []
        if _watched(str(c.get("name", ""))) and c.get("status") != "operational"
    ]
    incidents = [str(i.get("name", "")) for i in payload.get("incidents") or []]
    if not down:
        return ForgeStatus(False, "", fetched_at)
    return ForgeStatus(True, incidents[0] if incidents else down[0], fetched_at)


class StatusReader:
    """Edge: holds the single cache slot; fetch and clock are injected."""

    def __init__(self, fetch: Callable[[], dict], clock: Callable[[], float]) -> None:
        self._fetch = fetch
        self._clock = clock
        self._cache: ForgeStatus | None = None

    def read(self) -> ForgeStatus:
        now = self._clock()
        cached = self._cache
        if cached is not None and now - cached.fetched_at < CACHE_SECONDS:
            return cached
        try:
            payload = self._fetch()
        except Exception:
            # An unreachable status page must never pause the chair: serve stale, else not degraded.
            return cached if cached is not None else ForgeStatus(False, "", now)
        self._cache = parse_github_summary(payload, now)
        return self._cache
