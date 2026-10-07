"""Pure core for `agent-tools setup doctor`: judges gathered Facts, never
gathers them. No file reads, no subprocess, no clock — the CLI edge (next
phase) builds a Facts mapping and hands it to `checks`."""

from __future__ import annotations

from collections.abc import Mapping

from agent_tools import records, route, schema

__all__ = ["checks", "exit_code", "parquet_line", "render"]

_MISSING = object()
_SKIPPED = "skipped: no profile"
_NOT_CHECKED = "not checked"
_NEXT_STEP = "next: run `cox setup` to write a profile, or `cox install` to fetch every component"


def _skip(check: str) -> dict:
    return {"check": check, "ok": False, "detail": _SKIPPED}


def _not_checked(check: str) -> dict:
    return {"check": check, "ok": False, "detail": _NOT_CHECKED}


def _git_row(facts: Mapping) -> dict:
    version = facts.get("git_version", _MISSING)
    if version is _MISSING:
        return _not_checked("git")
    if isinstance(version, str) and version:
        return {"check": "git", "ok": True, "detail": version}
    return {"check": "git", "ok": False,
            "detail": "missing: install git (coxswain needs git; gh is needed only for the github forge)"}


def _forge_row(facts: Mapping) -> dict:
    name = facts.get("forge", _MISSING)
    if name is _MISSING:
        return _not_checked("forge")
    if name == "local":
        return {"check": "forge", "ok": True, "detail": "local: plain git, no pull-request host"}
    if name == "github":
        auth = facts.get("gh_auth", _MISSING)
        if auth is _MISSING:
            return _not_checked("forge")
        if auth is True:
            return {"check": "forge", "ok": True, "detail": "github: gh authenticated"}
        return {"check": "forge", "ok": False,
                "detail": "github: gh missing or not logged in (run `gh auth login`), or set forge: local in the profile"}
    if name == "auto":
        auth = facts.get("gh_auth", _MISSING)
        if auth is _MISSING:
            return _not_checked("forge")
        if auth is True:
            return {"check": "forge", "ok": True, "detail": "auto: github-origin repos through gh (authenticated), the rest local"}
        return {"check": "forge", "ok": False,
                "detail": "auto: gh missing or not logged in, so github-origin repos cannot land (run `gh auth login`)"}
    found = facts.get("forge_found", _MISSING)
    if found is _MISSING:
        return _not_checked("forge")
    if found is True:
        return {"check": "forge", "ok": True, "detail": f"{name}: installed"}
    return {"check": "forge", "ok": False, "detail": f"no forge named {name} installed"}


def _profile_row(facts: Mapping) -> tuple[dict, bool, dict | None]:
    """Returns the row, whether the profile itself failed (as opposed to
    simply not having been checked, which is what triggers the cascade), and
    the parsed profile (or None) so later rows can check against what the
    profile actually configures rather than only what got gathered."""
    path = facts.get("profile_path", "<profile>")
    text = facts.get("profile_text", _MISSING)
    if text is _MISSING:
        return _not_checked("profile"), False, None
    if text is None:
        return {"check": "profile", "ok": False, "detail": f"missing: {path}"}, True, None
    try:
        parsed = route.parse_profile(text)
    except route.ProfileError as err:
        return {"check": "profile", "ok": False, "detail": str(err)}, True, None
    return {"check": "profile", "ok": True, "detail": path}, False, parsed


def _unknown_key_rows(text: str | None, parsed: dict | None) -> list[dict]:
    """One passing row flagged `warn` per unknown top-level key. None when the profile did not parse."""
    if parsed is None or text is None:
        return []
    return [
        {"check": "profile", "ok": True, "warn": True, "detail": f"unknown key {key} at line {lineno}"}
        for key, lineno in route.unknown_keys(text)
    ]


def _expected_paths(parsed: Mapping) -> set[str]:
    """The path strings the profile configures: the same keys `paths_exist`
    is meant to carry, per Facts. Used to catch a path the profile names
    that the edge never gathered, not just one it gathered and found missing.

    Contract the edge must honour: `paths_exist` is keyed by the exact string
    the profile carries (e.g. a literal `~/foo`), not a normalised or
    expanded form. `cli.py`'s existing `Path(...).expanduser()` convention
    must not be used to build these keys, or an installed machine whose
    profile uses `~` will show every one of its own paths as "not checked"
    here."""
    singles = (parsed.get(k) for k in ("cartridges_dir", "provider_profile", "harness_dir", "workspace_dir"))
    roots = parsed.get("skills_roots") or []
    return {p for p in (*singles, *roots) if p}


def _paths_rows(facts: Mapping, cascade: bool, parsed: dict | None) -> list[dict]:
    """One row per configured path that is missing or never gathered; a
    single ok row naming the count when every configured path is present."""
    if cascade:
        return [_skip("profile paths")]
    paths = facts.get("paths_exist", _MISSING)
    if paths is _MISSING:
        return [_not_checked("profile paths")]
    if not paths:
        return [{"check": "profile paths", "ok": False, "detail": "no paths were configured to check"}]
    missing = sorted(p for p, exists in paths.items() if not exists)
    if missing:
        return [{"check": "profile paths", "ok": False, "detail": f"missing: {p}"} for p in missing]
    if parsed is not None:
        ungathered = sorted(_expected_paths(parsed) - set(paths))
        if ungathered:
            return [{"check": "profile paths", "ok": False, "detail": f"not checked: {p}"} for p in ungathered]
    return [{"check": "profile paths", "ok": True, "detail": f"{len(paths)} paths present"}]


def _flag_row(check: str, facts: Mapping, key: str, cascade: bool, missing_detail: str) -> dict:
    if cascade:
        return _skip(check)
    val = facts.get(key, _MISSING)
    if val is _MISSING:
        return _not_checked(check)
    return {"check": check, "ok": bool(val), "detail": "ok" if val else missing_detail}


def _none_ok_row(check: str, facts: Mapping, key: str, cascade: bool) -> dict:
    if cascade:
        return _skip(check)
    val = facts.get(key, _MISSING)
    if val is _MISSING:
        return _not_checked(check)
    if val is None:
        return {"check": check, "ok": True, "detail": "ok"}
    return {"check": check, "ok": False, "detail": str(val)}


def _overlay_row(facts: Mapping, cascade: bool) -> dict:
    if cascade:
        return _skip("project overlay")
    errors = facts.get("overlay_errors", _MISSING)
    if errors is _MISSING:
        return _not_checked("project overlay")
    if errors is None:
        return {"check": "project overlay", "ok": True, "detail": "no project overlay"}
    if not errors:
        return {"check": "project overlay", "ok": True, "detail": "ok"}
    return {"check": "project overlay", "ok": False, "detail": str(errors[0])}


def _skills_row(facts: Mapping, cascade: bool, parsed: dict | None) -> dict:
    if cascade:
        return _skip("skills")
    counts = facts.get("skill_roots_indexed", _MISSING)
    if counts is _MISSING:
        return _not_checked("skills")
    if not counts:
        return {"check": "skills", "ok": False, "detail": "no skill roots were configured to check"}
    empty = sorted(root for root, n in counts.items() if not n)
    if empty:
        return {"check": "skills", "ok": False, "detail": "0 skills: " + ", ".join(empty)}
    if parsed is not None:
        ungathered = sorted(set(parsed.get("skills_roots") or []) - set(counts))
        if ungathered:
            return {"check": "skills", "ok": False, "detail": "not indexed: " + ", ".join(ungathered)}
    return {"check": "skills", "ok": True, "detail": f"{len(counts)} roots indexed"}


def _provider_row(facts: Mapping, cascade: bool) -> dict:
    if cascade:
        return _skip("provider")
    on_path = facts.get("provider_on_path", _MISSING)
    if on_path is _MISSING:
        return _not_checked("provider")
    # `command` distinguishes three gathered states: an explicit None means
    # the edge tried to read `command:` from the provider profile and could
    # not; `_MISSING` means that fact simply was not gathered, which is not
    # the same claim and must not be reported as "unreadable".
    command = facts.get("provider_command", _MISSING)
    if command is None:
        return {"check": "provider", "ok": False, "detail": "provider command unreadable from provider profile"}
    display_command = command if command is not _MISSING else "<command>"
    if on_path is None:
        return {"check": "provider", "ok": False, "detail": f"provider on PATH unknown: {display_command}"}
    if not on_path:
        return {"check": "provider", "ok": False, "detail": f"not on PATH: {display_command}"}
    version = facts.get("provider_version") or display_command
    return {"check": "provider", "ok": True, "detail": version}


_TOOLS_PLUGIN_GROUPS = ("sources", "forges", "trackers")
_HARNESS_PLUGIN_GROUPS = ("system_one", "runners")


def _plugin_parts(found: Mapping | None, groups: tuple) -> list[str]:
    """One `group: a, b` part per group: `none` when empty, `not checked` when
    the whole fact is absent. Keys in `found` carry the `coxswain.` prefix."""
    return [
        f"{g}: not checked" if found is None
        else f"{g}: " + (", ".join(found.get(f"coxswain.{g}") or ()) or "none")
        for g in groups
    ]


def _plugins_row(facts: Mapping) -> dict:
    """Informs, never judges: always ok, and never skipped by a profile failure."""
    parts = [
        *_plugin_parts(facts.get("plugins_tools"), _TOOLS_PLUGIN_GROUPS),
        *_plugin_parts(facts.get("plugins_harness"), _HARNESS_PLUGIN_GROUPS),
    ]
    return {"check": "plugins", "ok": True, "detail": "; ".join(parts)}


def _store_row(facts: Mapping, cascade: bool) -> dict:
    if cascade:
        return _skip("store")
    store = facts.get("store", _MISSING)
    if store is _MISSING:
        return _not_checked("store")
    if store.get("reachable"):
        return {"check": "store", "ok": True, "detail": f"{store.get('kind')}, {store.get('runs')} runs"}
    if store.get("fresh"):
        return {"check": "store", "ok": True, "detail": f"{store.get('kind')}, no runs yet (the first run creates it)"}
    return {"check": "store", "ok": False, "detail": store.get("error") or "the store did not answer"}


def _workspace_row(facts: Mapping, cascade: bool) -> dict:
    if cascade:
        return _skip("workspace")
    dirs = facts.get("workspace_dirs", _MISSING)
    if dirs is _MISSING:
        return _not_checked("workspace")
    if not dirs:
        return {"check": "workspace", "ok": False, "detail": "no workspace dirs were configured to check"}
    missing = sorted(d for d, exists in dirs.items() if not exists)
    if missing:
        return {"check": "workspace", "ok": False, "detail": "missing: " + ", ".join(missing)}
    return {"check": "workspace", "ok": True, "detail": f"{len(dirs)} dirs present"}


def _schema_row(facts: Mapping, cascade: bool) -> dict:
    if cascade:
        return _skip("schema")
    state, detail = schema.status(facts.get("schema_versions", {}))
    return {"check": "schema", "ok": state == "ok", "detail": detail}


def _cast_row(facts: Mapping, cascade: bool) -> dict:
    """A `cast_seats` fact absent from `facts` passes rather than fails,
    unlike every row above it: the edge that gathers it is not yet wired,
    and every real invocation today omits it."""
    if cascade:
        return _skip("cast")
    seats = facts.get("cast_seats", _MISSING)
    if seats is _MISSING:
        return {"check": "cast", "ok": True, "detail": "not gathered"}
    if not seats:
        return {"check": "cast", "ok": True, "detail": "0 seats"}
    missing = sorted(seat for seat, s in seats.items() if s.get("enabled", True) and not s.get("installed"))
    if missing:
        return {"check": "cast", "ok": False, "detail": "missing: " + ", ".join(missing)}
    present = sorted(seat for seat, s in seats.items() if not s.get("enabled", True) and s.get("installed"))
    if present:
        return {"check": "cast", "ok": False, "detail": "present: " + ", ".join(present)}
    return {"check": "cast", "ok": True, "detail": f"{len(seats)} seats"}


def checks(facts: Mapping) -> list[dict]:
    """Judge a Facts mapping. Returns rows `{"check", "ok", "detail"}` in a
    fixed check order: git, forge, profile, profile paths, harness venv, core
    importable, cartridge, project overlay, skills, provider, plugins,
    store, workspace, schema, cast. A fact that was never gathered fails as "not checked",
    except `cast` which passes when ungathered; a profile that fails to parse
    fails every row after it as "skipped: no profile"; an empty collection
    where paths, skill roots or workspace dirs belong fails naming that
    nothing was configured to check, rather than passing vacuously."""
    profile_row, cascade, parsed = _profile_row(facts)
    return [
        _git_row(facts),
        _forge_row(facts),
        profile_row,
        *_unknown_key_rows(facts.get("profile_text"), parsed),
        *_paths_rows(facts, cascade, parsed),
        _flag_row("harness venv", facts, "harness_python_exists", cascade, "venv missing"),
        _none_ok_row("core importable", facts, "core_import", cascade),
        _none_ok_row("cartridge", facts, "cartridge_load", cascade),
        _overlay_row(facts, cascade),
        _skills_row(facts, cascade, parsed),
        _provider_row(facts, cascade),
        _plugins_row(facts),
        _store_row(facts, cascade),
        _workspace_row(facts, cascade),
        _schema_row(facts, cascade),
        _cast_row(facts, cascade),
    ]


def exit_code(rows: list[dict]) -> int:
    """0 when every row is ok, else 1."""
    return 0 if all(row["ok"] for row in rows) else 1


_PARQUET_FIXES = {
    "pyarrow missing": "install the parquet extra",
    # Only reported after pyarrow imported, so installing the extra would change nothing.
    "root unreachable": "create the traces directory, or correct traces_url in the provider profile",
}


def parquet_line(readable: bool, reason: str, endpoint: str | None = None) -> str:
    """The one informational line for Parquet trace reads. Never a row, so it never affects `exit_code`.
    The fix is chosen by `reason`; an unknown reason is shown with no fix rather than a wrong one.
    An `endpoint`, the object_store block's, is named after the state."""
    via = f" via {endpoint}" if endpoint else ""
    if readable:
        return f"parquet traces: readable{via}"
    fix = _PARQUET_FIXES.get(reason)
    state = f"parquet traces: not readable ({reason}){via}"
    return f"{state}; fix: {fix}" if fix else state


def render(rows: list[dict]) -> str:
    """A text table plus a one-line summary of how many rows passed. A `warn` row passes and is counted apart."""
    display = [{"check": r["check"], "ok": "warn" if r.get("warn") else "ok" if r["ok"] else "FAIL", "detail": r["detail"]} for r in rows]
    n_warn = sum(1 for r in rows if r.get("warn"))
    n_ok = sum(1 for r in rows if r["ok"]) - n_warn
    n_fail = sum(1 for r in rows if not r["ok"])
    table = records.format_table(display, ["check", "ok", "detail"])
    warned = f", {n_warn} warning" if n_warn else ""
    summary = f"{table}\ndoctor: {n_ok} ok, {n_fail} failing{warned}"
    profile_missing = any(r["check"] == "profile" and not r["ok"] and r["detail"].startswith("missing:") for r in rows)
    return f"{summary}\n{_NEXT_STEP}" if profile_missing else summary
