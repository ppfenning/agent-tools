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
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import chair, dash_feed, lake_config, lake_doctor, route, route_drift, run_store, store_url
from agent_tools.chair_read_housekeeping import read_last_housekeeping
from agent_tools.chair_read_lease import lease_record

NO_PROFILE = "no provider profile given; the lake panels read the operator's profile, never defaults"

# mirrors cli.DEFAULT_PROFILE / cli._profile_path; cli.py is out of bounds for this module to import
DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"

# The age of a housekeeping that never ran. 0.0 would read as just-run; JSON has no infinity, so a large finite float.
NEVER_AGE_HOURS = 999999.0


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


def _parse_now(text: str) -> datetime:
    """A UTC-aware datetime from an ISO stamp; a trailing `Z` and a naive stamp both read as UTC."""
    parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hosts(machines: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The schema-1 `hosts`: each feed machine's name, state, lanes in use and capacity."""
    return [
        {
            "name": str(m.get("name") or ""),
            "state": str(m.get("state") or ""),
            "lanes_in_use": int(m.get("lanes_in_use") or 0),
            "capacity": int(m.get("capacity") or 0),
        }
        for m in machines
    ]


def _login_detail(login_ok: bool | None, checked_at: str) -> str:
    when = f", checked {checked_at}" if checked_at else ""
    if login_ok is None:
        return "login not checked"
    return ("logged in" if login_ok else "logged out") + when


def _logins(machines: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The schema-1 `logins`: one per machine, provider "claude". A login still unchecked (`login_ok` None) is not ok."""
    return [
        {
            "provider": "claude",
            "host": str(m.get("name") or ""),
            "ok": m.get("login_ok") is True,
            "detail": _login_detail(m.get("login_ok"), str(m.get("login_checked_at") or "")),
        }
        for m in machines
    ]


def _store(result: Mapping[str, Any], latency_ms: float) -> dict:
    """The schema-1 `store` from the timed read's `{"ok", "detail"}` and its elapsed milliseconds."""
    return {"ok": bool(result.get("ok")), "detail": str(result.get("detail") or ""), "latency_ms": int(latency_ms)}


def _chair_lease(row: Mapping[str, Any] | None, now: datetime) -> dict:
    """The schema-1 `chair_lease` from the lease row `{"holder", "expires_at"}` (None when there is none).

    `ok` means held and unexpired at `now`; a released or missing lease has an empty holder or expiry and is not ok."""
    record = lease_record(row, now)
    return {
        "holder": str(record["holder"]),
        "expires_at": str(row.get("expires_at") or "") if row else "",
        "ok": not record["released"] and not record["stale"],
    }


def _housekeeping_v1(last_run_at: str | None, now: datetime) -> dict:
    """The schema-1 `housekeeping`. A housekeeping that never ran, or whose stamp does not parse, reads as
    `NEVER_AGE_HOURS`, so it renders as overdue rather than just-run and `age_hours` keeps its JSON type."""
    if not last_run_at:
        return {"last_run_at": "", "age_hours": NEVER_AGE_HOURS}
    try:
        age = (now - _parse_now(last_run_at)).total_seconds() / 3600
    except ValueError:
        return {"last_run_at": str(last_run_at), "age_hours": NEVER_AGE_HOURS}
    return {"last_run_at": str(last_run_at), "age_hours": max(0.0, round(age, 2))}


def schema_1(
    machines: Sequence[Mapping[str, Any]],
    lease_row: Mapping[str, Any] | None,
    store_result: Mapping[str, Any],
    latency_ms: float,
    last_run_at: str | None,
    at: str,
) -> dict:
    """Pure. The schema-1 keys coxtop parses, from literals: the feed's machines, the chair lease row, the timed store
    read's result and milliseconds, the last housekeeping stamp, and the snapshot time `at`, the one clock."""
    now = _parse_now(at)
    return {
        "schema": 1,
        "kind": "health",
        "at": at,
        "hosts": _hosts(machines),
        "logins": _logins(machines),
        "store": _store(store_result, latency_ms),
        "chair_lease": _chair_lease(lease_row, now),
        "housekeeping": _housekeeping_v1(last_run_at, now),
    }


def _routing_profile() -> dict | None:
    """Edge. The routing profile `cox dash --feed` hands `gather_feed`, read from `$AGENT_TOOLS_PROFILE` or `DEFAULT_PROFILE`;
    None when it is missing or does not parse."""
    text = _read(Path(os.environ.get("AGENT_TOOLS_PROFILE") or DEFAULT_PROFILE).expanduser())
    if text is None:
        return None
    try:
        return dict(route.parse_profile(text))
    except route.ProfileError:
        return None


def _machines(runs_dir: Path, work_dir: Path, now: str, profile: Mapping[str, Any] | None) -> tuple[list[dict], str | None]:
    """Edge. `(machines, error)`: the feed's `machines` from `dash_feed.gather_feed`, given the same routing profile the
    feed gets, so the local host's capacity comes from the team cartridge as on coxtop's own board. Without it,
    `chair_capacity` skips the cartridge and falls back to 3. A raising feed is `([], reason)`, never an empty fleet."""
    try:
        return list(dash_feed.gather_feed(Path(runs_dir), Path(work_dir), now, dict(profile) if profile else None)["machines"]), None
    except Exception as exc:  # reported in `errors`, so a broken feed does not read as a fleet with no hosts
        return [], f"machines: {type(exc).__name__}: {exc}"


def _timed_store_read(runs_dir: Path, clock: Callable[[], float] = time.monotonic) -> tuple[dict, int]:
    """Edge. One store open through `run_store._open`, as every store reader does: `({"ok", "detail"}, elapsed_ms)`.

    No store, or one that cannot be opened, is `ok` False with the reason; it never raises."""
    started = clock()
    try:
        opened = run_store._open(Path(runs_dir))
        if opened is None:
            result = {"ok": False, "detail": "no store under the runs dir"}
        else:
            opened[0].close()
            result = {"ok": True, "detail": "store opened"}
    except Exception as exc:  # an unreachable store is this panel's finding
        result = {"ok": False, "detail": str(exc)}
    return result, round((clock() - started) * 1000)


def _lease_row(runs_dir: Path, clock: Callable[[], float] = time.monotonic) -> tuple[dict | None, str | None]:
    """Edge. `(row, error)`: the chair lease as `{"holder", "expires_at"}` from `run_store._lease_table`, which selects
    `expires_at`; `chair_read_lease.read_lease` reads the same row but drops it. No row is `(None, None)`; a raising
    read or a row of another shape is `(None, reason)`."""
    try:
        row = run_store._lease_table(str(runs_dir), int(clock())).get(chair.LEASE_NAME)
        return (None, None) if row is None else ({"holder": row[0], "expires_at": row[1]}, None)
    except Exception as exc:  # reported in `errors`, so a broken read does not read as a released lease
        return None, f"chair_lease: {type(exc).__name__}: {exc}"


def build(
    runs_dir: Path,
    work_dir: Path,
    now: str,
    *,
    provider: Mapping[str, Any] | None = None,
    harness: Path | None = None,
) -> dict:
    """The five panels behind `cox dash --detail health`, each `_safe` so one's failure never reaches another's key,
    plus the schema-1 keys coxtop parses (`schema_1`).

    `provider` is the operator's provider profile and `harness` its routing profile's harness python.
    A caller who already has them (a test, or a future caller with its own routing profile in hand)
    passes them directly and they are used as given. A caller with neither — `cox dash --detail
    health`, which calls `build(runs_dir, work_dir, now)` — gets them resolved here, at this module's
    own edge, via `_resolve_profile`; only the keyword left `None` is filled in, so a caller who
    passes `provider={}` on purpose is never second-guessed. `now` is the caller's stamp and the
    snapshot's `at`; only the edges below read a clock, to time the store read."""
    if provider is None or harness is None:
        resolved_provider, resolved_harness = _resolve_profile()
        provider = resolved_provider if provider is None else provider
        harness = resolved_harness if harness is None else harness
    housekeeping_panel = _safe(lambda: _housekeeping(runs_dir))
    store_result, latency_ms = _timed_store_read(runs_dir)
    machines, machines_error = _machines(runs_dir, work_dir, now, _routing_profile())
    lease_row, lease_error = _lease_row(runs_dir)
    return {
        "store_drift": _safe(lambda: _store_drift(runs_dir, work_dir)),
        "lake_doctor": _safe(lambda: _lake_doctor(runs_dir, provider)),
        "object_store": _safe(lambda: _object_store(runs_dir, provider, harness)),
        "last_backup": _safe(_last_backup),
        # `housekeeping` was this panel's `{"ok", "last_at"}` until schema 1 took the name for `{last_run_at, age_hours}`.
        # coxtop reads the schema-1 object, so the panel moved here; only this module's tests read it.
        "housekeeping_panel": housekeeping_panel,
        # A failed edge read leaves its schema-1 key empty or not ok; this names which read failed and why.
        "errors": [e for e in (machines_error, lease_error) if e],
        **schema_1(
            machines,
            lease_row,
            store_result,
            latency_ms,
            housekeeping_panel.get("last_at"),
            now,
        ),
    }
