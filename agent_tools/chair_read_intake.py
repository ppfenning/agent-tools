"""Readers for the chair's `intake` and `sources_configured` facts.

The core is pure. The edge lists intake with `route.intake_entries` and reads the profile with `route.parse_profile`.
"""
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import route


def queued_oldest_first(groups: Mapping[str, Sequence[Mapping]], mtimes: Mapping[str, float]) -> list[str]:
    """Paths of the `queued` group of `route.intake_groups`, oldest mtime first; equal mtimes fall back to path."""
    return [path for _, path in sorted((mtimes[row["path"]], row["path"]) for row in groups["queued"])]


def has_sources(sources: Sequence[str]) -> bool:
    return len(sources) > 0


def read_intake(ws: Path) -> list[str]:
    """Edge. Queued intake paths under `ws/intake`, oldest first; none when the directory is absent."""
    root = ws / "intake"
    paths = sorted(root.glob("*.md")) + sorted((root / "done").glob("*.md"))
    files = {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in paths}
    groups = route.intake_groups(route.intake_entries(files), _initiatives(ws))
    return queued_oldest_first(groups, {row["path"]: (ws / row["path"]).stat().st_mtime for row in groups["queued"]})


def _initiatives(ws: Path) -> list[dict]:
    """Initiative rows as `route.intake_groups` reads them. `done` is False: it never decides membership of `queued`."""
    return [
        {"id": p.parent.name, "done": False, "text": p.read_text(encoding="utf-8")}
        for p in sorted((ws / "work").glob("*/initiative.md"))
    ]


def read_sources_configured(profile_path: Path) -> bool:
    """Edge. True when the profile's `sources` block names any source; an unreadable profile configures none."""
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return has_sources(list(route.parse_profile(text).get("sources", {})))
