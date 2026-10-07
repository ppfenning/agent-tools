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


def intake_from_rows(rows: Sequence[Mapping]) -> list[Mapping]:
    """Queued intake rows naming no initiative. `route.intake_groups` never queues one that names an initiative, done or not.

    unknown: an `initiative.md` whose `intake:` field names the entry also unqueues it; rows carry no initiative.md, so that is not checked.
    """
    return [r for r in rows if r["kind"] == "intake" and r["state"] == "queued" and r["extra"].get("initiative") is None]


def read_intake(ws: Path, initiative_texts: Mapping[Path, str] | None = None) -> list[str]:
    """Edge. Queued intake paths under `ws/intake`, oldest first; none when the directory is absent.

    `initiative_texts` (path to text of each `work/*/initiative.md`) stands in for reading them when a tick already holds them."""
    root = ws / "intake"
    paths = sorted(root.glob("*.md")) + sorted((root / "done").glob("*.md"))
    files = {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in paths}
    groups = route.intake_groups(route.intake_entries(files), _initiatives(ws, initiative_texts))
    return queued_oldest_first(groups, {row["path"]: (ws / row["path"]).stat().st_mtime for row in groups["queued"]})


def _initiatives(ws: Path, texts: Mapping[Path, str] | None = None) -> list[dict]:
    """Initiative rows as `route.intake_groups` reads them. `done` is False: it never decides membership of `queued`."""
    held = texts if texts is not None else {p: p.read_text(encoding="utf-8") for p in sorted((ws / "work").glob("*/initiative.md"))}
    return [{"id": p.parent.name, "done": False, "text": text} for p, text in held.items()]


def read_sources_configured(profile_path: Path) -> bool:
    """Edge. True when the profile's `sources` block names any source; an unreadable profile configures none."""
    try:
        text = profile_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return has_sources(list(route.parse_profile(text).get("sources", {})))
