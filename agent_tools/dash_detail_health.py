"""The data behind `cox dash --detail health`: store drift, lake doctor's findings, object-store
reachability, the last backup, and housekeeping's last run. Each panel calls the public functions
behind its own `cox` command directly, never `subprocess`, and `_safe` folds a raising reader into
its own key's error so no panel blocks another.

`_last_backup` always reports unavailable: no backup reader exists anywhere in agent_tools/ (checked
with `rg -n "backup" agent_tools/`), though this module's ticket assumed one did.

`build` accepts `provider`/`harness` as keywords for tests that want to hand it a literal profile
directly. Its sole caller (`cox dash --detail health`) calls `build(runs_dir, work_dir, now)` with
none of those, so when either is left `None`, `build` resolves it itself at its own edge, the way
`cli._lake_provider` and `cli._parquet_traces_line` do: read the routing profile at
`$AGENT_TOOLS_PROFILE` or `~/.config/agent-tools/profile.yaml`, `route.parse_profile` it, then
`store_url.read_provider_profile` its named `provider_profile` and `run_store.harness_python` it
for the harness. A missing or unreadable routing profile leaves both `None`, and the two lake panels
report `NO_PROFILE`, same as today."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent_tools import lake_config, lake_doctor, route, route_drift, run_store, store_url
from agent_tools.chair_read_housekeeping import read_last_housekeeping

NO_PROFILE = "no provider profile given; the lake panels read the operator's profile, never defaults"

# mirrors cli.DEFAULT_PROFILE / cli._profile_path; cli.py is out of bounds for this module to import
DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"


def _safe(fn: Callable[[], dict]) -> dict:
    """`fn`'s own dict, or `{"ok": False, "error": str(exc)}` when it raises."""
    try:
        return fn()
    except Exception as exc:  # a reader's own failure is its own finding, not a crash
        return {"ok": False, "error": str(exc)}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_profile() -> tuple[Mapping[str, Any] | None, Path | None]:
    """`(provider, harness)` read at this module's own edge, used only for whichever of `build`'s
    `provider`/`harness` keywords the caller left `None`. `(None, None)` when the routing profile at
    `$AGENT_TOOLS_PROFILE` or `DEFAULT_PROFILE` is missing or fails `route.parse_profile`, which
    leaves the two lake panels reporting `NO_PROFILE` rather than a silent default lake."""
    path = Path(os.environ.get("AGENT_TOOLS_PROFILE") or DEFAULT_PROFILE).expanduser()
    text = _read(path)
    if text is None:
        return None, None
    try:
        routing_profile = route.parse_profile(text)
    except route.ProfileError:
        return None, None
    provider_path = routing_profile.get("provider_profile")
    provider = store_url.read_provider_profile(provider_path) if provider_path else {}
    return provider, run_store.harness_python(routing_profile)


def _work_items(ws: Path) -> list[dict]:
    """Every task under `ws/work` through `route.work_item`, so a missing `state` reads "todo" as in `cox route drift`."""
    return [
        route.work_item(route.parse_frontmatter(text)[0], initiative=path.parent.parent.name, phase_dir=path.parent.name, stem=path.stem)
        for path in sorted((ws / "work").glob("*/*/*.md"))
        if path.name != "initiative.md"
        for text in [_read(path)]
        if text is not None
    ]


def _intake_files(ws: Path, items: list[dict]) -> list[tuple[str, str]]:
    """(path, group) per intake entry, built as `cox route drift` builds it; empty with no `intake/` dir."""
    root = ws / "intake"
    if not root.is_dir():
        return []
    paths = sorted(root.glob("*.md")) + sorted((root / "done").glob("*.md"))
    files = {str(p.relative_to(root)): t for p in paths if (t := _read(p)) is not None}
    texts = {d.name: _read(d / "initiative.md") or "" for d in sorted((ws / "work").glob("*")) if (d / "initiative.md").is_file()}
    states = route.initiative_states(sorted(texts), items)
    initiatives = [{"id": iid, "done": states[iid], "text": texts[iid]} for iid in texts]
    groups = route.intake_groups(route.intake_entries(files), initiatives)
    return [(entry["path"], name) for name, entries in groups.items() for entry in entries]


def _store_drift(runs_dir: Path, work_dir: Path) -> dict:
    """`{"ok": True, "drift": rows}`: `route_drift.drift` over tasks and intake, the four inputs `cox route drift` passes.

    `work_dir` is the workspace root that holds `work/` and `intake/`."""
    ws = Path(work_dir)
    items = _work_items(ws)
    rows = run_store.read_queue(Path(runs_dir))
    found = route_drift.drift(
        [(item["initiative"], item["id"], item["state"]) for item in items],
        [r for r in rows if r.get("kind") == "task"],
        _intake_files(ws, items),
        [r for r in rows if r.get("kind") == "intake"],
    )
    return {"ok": True, "drift": found}


def _lake_doctor(runs_dir: Path, provider: Mapping[str, Any] | None) -> dict:
    """`lake_doctor.run_checks` over the lake the operator's provider profile names; an error without a profile."""
    if provider is None:
        return {"ok": False, "error": NO_PROFILE}
    checks = lake_doctor.run_checks(lake_config.resolve_lake(provider, Path(runs_dir)))
    return {
        "ok": True,
        "verdict": lake_doctor.verdict(checks),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
    }


def _object_store(runs_dir: Path, provider: Mapping[str, Any] | None, harness: Path | None) -> dict:
    """A live probe of the traces root the provider profile names, through `run_store.parquet_readable`.

    The same two arguments `cli._parquet_traces_line` passes: the traces root and the routing
    profile's harness python. Without the harness, a pyarrow-less install reads as unreadable
    even when the harness venv could read it, so `harness` must be the caller's resolved
    `run_store.harness_python(routing_profile)`, not left `None`."""
    if provider is None:
        return {"ok": False, "error": NO_PROFILE}
    root = store_url.profile_traces_root(provider, Path(runs_dir))
    check = run_store.parquet_readable(root, harness)
    endpoint = root.object_store.get("endpoint") if root.object_store else None
    return {"ok": check.readable, "reachable": check.readable, "reason": check.reason, "endpoint": endpoint}


def _last_backup() -> dict:
    """Always unavailable: no backup reader exists anywhere in agent_tools/."""
    return {"ok": False, "error": "no backup reader exists in agent_tools/"}


def _housekeeping(runs_dir: Path) -> dict:
    """`{"ok": True, "last_at": ...}` from `chair_read_housekeeping.read_last_housekeeping`."""
    return {"ok": True, "last_at": read_last_housekeeping(Path(runs_dir))}


def build(
    runs_dir: Path,
    work_dir: Path,
    now: str,
    *,
    provider: Mapping[str, Any] | None = None,
    harness: Path | None = None,
) -> dict:
    """The five panels behind `cox dash --detail health`, each `_safe` so one's failure never reaches another's key.

    `provider` is the operator's provider profile and `harness` its routing profile's harness python.
    A caller who already has them (a test, or a future caller with its own routing profile in hand)
    passes them directly and they are used as given. A caller with neither — `cox dash --detail
    health`, which calls `build(runs_dir, work_dir, now)` — gets them resolved here, at this module's
    own edge, via `_resolve_profile`; only the keyword left `None` is filled in, so a caller who
    passes `provider={}` on purpose is never second-guessed. `now` is the caller's stamp; no panel
    reads the clock."""
    del now  # the snapshot's timestamp is the caller's concern; no panel here reads the clock
    if provider is None or harness is None:
        resolved_provider, resolved_harness = _resolve_profile()
        provider = resolved_provider if provider is None else provider
        harness = resolved_harness if harness is None else harness
    return {
        "store_drift": _safe(lambda: _store_drift(runs_dir, work_dir)),
        "lake_doctor": _safe(lambda: _lake_doctor(runs_dir, provider)),
        "object_store": _safe(lambda: _object_store(runs_dir, provider, harness)),
        "last_backup": _safe(_last_backup),
        "housekeeping": _safe(lambda: _housekeeping(runs_dir)),
    }
