"""`cox settings get` and `cox settings set`: the settings rows of the cartridge and the profile.

Parsing, grouping and formatting are pure functions of data. The edge, `git_tracked`, `run_get`,
`git_user_name`, `git_commit` and `run_set`, only reads files, asks git, writes and prints. The cartridge is `<cartridges_dir>/<team>/cartridge.yaml`,
both named by the profile, and its text is parsed by `chair_cap._load` (yaml.safe_load, `{}` on a
broken or non-mapping document).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from agent_tools import route, run_store, settings_model, settings_plan
from agent_tools.chair_cap import _load as _load_cartridge
from agent_tools.settings_builds import builds_rows
from agent_tools.settings_hosts import host_rows
from agent_tools.settings_housekeeping import housekeeping_rows
from agent_tools.settings_model import SettingRow
from agent_tools.settings_model_rows import model_tier_rows
from agent_tools.settings_sections.defaults import default_rows

Row = SettingRow | dict
Grouped = dict[str, list[Row]]

# unknown: the cartridge key that holds the role-to-model map. `settings_model_rows` assumes a mapping of
# role to {model, tier}; no code in the repo reads one, so `models` is the assumed top-level key.
MODEL_MAP_KEY = "models"


def parse_documents(cartridge_text: str | None, profile_text: str | None) -> tuple[dict, dict]:
    """The cartridge and profile dicts; a missing or unreadable document is `{}`."""
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError:
        profile = {}
    return _load_cartridge(cartridge_text), profile


def cartridge_path_for(profile: dict) -> Path | None:
    """`<cartridges_dir>/<team>/cartridge.yaml`; None when the profile names no team or no directory."""
    cartridges_dir, team = profile.get("cartridges_dir"), profile.get("team")
    if not (cartridges_dir and isinstance(team, str) and team):
        return None
    return Path(cartridges_dir).expanduser() / team / settings_model.CARTRIDGE_FILE


def pat_only_for(row: SettingRow) -> bool:
    """Whether the registry marks this row pat_only; a key the registry does not know is not."""
    setting = settings_model.setting_for(row.scope, row.key)
    return setting.pat_only if setting is not None else False


def _row_json(row: Row) -> dict:
    """A builder's dict row already carries `pat_only`; a `SettingRow` takes it from the registry."""
    if isinstance(row, dict):
        return row
    return {
        "section": row.section,
        "scope": row.scope,
        "key": row.key,
        "value": row.value,
        "source_file": row.source_file,
        "tracked": row.tracked,
        "pat_only": pat_only_for(row),
    }


def to_json(grouped: Grouped) -> dict:
    """`{"sections": [{"name": ..., "rows": [...]}]}`, sections in the order of `grouped`, empty ones kept."""
    return {"sections": [{"name": name, "rows": [_row_json(r) for r in rows]} for name, rows in grouped.items()]}


def _row_line(row: Row) -> str:
    f = _row_json(row)
    where = "tracked" if f["tracked"] else "local"
    marker = "  [pat-only]" if f["pat_only"] else ""
    return f"  {f['scope']}:{f['key']} = {json.dumps(f['value'], default=str)}  ({f['source_file']}, {where}){marker}"


def render_text(grouped: Grouped) -> str:
    """One block per non-empty section: its name, then one line per row; blocks are separated by a blank line."""
    blocks = [[name, *(_row_line(r) for r in rows)] for name, rows in grouped.items() if rows]
    return "\n\n".join("\n".join(block) for block in blocks)


def _first_per_key(rows: list[Row]) -> list[Row]:
    """Drop a row whose `(scope, key)` an earlier row already holds."""
    keys = [(f["scope"], f["key"]) for f in map(_row_json, rows)]
    return [row for i, row in enumerate(rows) if keys.index(keys[i]) == i]


def host_capacities(rows: list[dict]) -> dict[str, object]:
    """`{name: capacity}` from `run_store.hosts` rows; a row without a name is dropped."""
    return {str(r["name"]): r.get("capacity") for r in rows if r.get("name")}


def sections(cartridge: dict, profile: dict, hosts: dict[str, object], tracked: dict[str, bool]) -> Grouped:
    """Registry rows with each builder's rows appended to its section. A `(scope, key)` already in a
    section keeps its first row, so the registry's rows win where a builder repeats them."""
    model_map = cartridge.get(MODEL_MAP_KEY)
    extra = {
        "lanes and machines": host_rows(hosts, profile, tracked),
        "builds and budgets": builds_rows(cartridge, settings_model.CARTRIDGE_FILE, tracked),
        "housekeeping": housekeeping_rows(profile, tracked),
        "models and tiers": model_tier_rows(
            model_map if isinstance(model_map, dict) else {}, tracked.get(settings_model.CARTRIDGE_FILE, False)
        ),
    }
    base = settings_model.rows(cartridge, profile, tracked)
    return {name: _first_per_key([*found, *extra.get(name, [])]) for name, found in base.items()}


def with_defaults(grouped: Grouped) -> Grouped:
    """`grouped` with each built-in default row after its section's own rows; a set key gets none."""
    defaults = default_rows([row for rows in grouped.values() for row in rows])
    return {
        name: [*rows, *(d for d in defaults if d["section"] == name)]
        for name, rows in grouped.items()
    }


def git_tracked(path: Path) -> bool:
    """Edge. True only when `git ls-files --error-unmatch` exits 0 for `path` from its own directory;
    a missing file, a directory outside a repository and a missing git binary are all False."""
    try:
        done = subprocess.run(
            ["git", "ls-files", "--error-unmatch", path.name],
            cwd=path.parent, capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


def _read_text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def run_get(profile_path: Path, as_json: bool) -> int:
    """Edge. Read the profile, then the cartridge it names, ask git about both, print the rows."""
    profile_text = _read_text_or_none(profile_path)
    _, profile = parse_documents(None, profile_text)
    cartridge_path = cartridge_path_for(profile)
    cartridge_text = _read_text_or_none(cartridge_path) if cartridge_path is not None else None
    cartridge, profile = parse_documents(cartridge_text, profile_text)
    tracked = {
        settings_model.PROFILE_FILE: git_tracked(profile_path),
        settings_model.CARTRIDGE_FILE: git_tracked(cartridge_path) if cartridge_path is not None else False,
    }
    workspace = profile.get("workspace_dir")
    hosts = host_capacities(run_store.hosts(Path(workspace).expanduser() / "runs")) if workspace else {}
    grouped = with_defaults(sections(cartridge, profile, hosts, tracked))
    print(json.dumps(to_json(grouped), default=str, indent=2) if as_json else render_text(grouped))
    return 0


def commit_message(key: str, value: str) -> str:
    """`settings: <key> = <value>`, with the value exactly as given on the command line."""
    return f"settings: {key} = {value}"


def commit_command(path_name: str, message: str) -> str:
    """The shell-quoted `git commit` that `--commit` runs for `path_name`; printed by `--dry-run`."""
    return f"git commit -m {shlex.quote(message)} -- {shlex.quote(path_name)}"


def target_path(scope: str, profile_path: Path, profile: dict) -> Path | None:
    """The file a `scope` edit rewrites; None for an unknown scope or a profile naming no cartridge."""
    if scope == "profile":
        return profile_path
    if scope == "cartridge":
        return cartridge_path_for(profile)
    return None


def _no_schema(_text: str) -> list[str]:
    # `core.cartridge.overlay_errors` is importable only inside the harness venv, so the cartridge is not schema-checked here.
    return []


def git_user_name(cwd: Path) -> str | None:
    """Edge. `git config user.name` run from `cwd`; None when unset, empty, or git cannot run."""
    try:
        done = subprocess.run(
            ["git", "config", "user.name"], cwd=cwd, capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def git_commit(path: Path, message: str) -> str | None:
    """Edge. Commit only `path` from its own directory; None on success, git's complaint on failure."""
    try:
        done = subprocess.run(
            ["git", "commit", "-m", message, "--", path.name],
            cwd=path.parent, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        return str(exc)
    return None if done.returncode == 0 else (done.stderr.strip() or done.stdout.strip() or "git commit failed")


def parse_host_capacity(key: str, value: str) -> tuple[str, int] | str:
    """The (host, n) of `<host>.capacity N`, or an error message. Host names may contain dots, so the split is on the last one."""
    host, _, field = key.rpartition(".")
    if not host or field != "capacity":
        return f"host key must be <host>.capacity, got {key!r}"
    try:
        return host, int(value)
    except ValueError:
        return f"host capacity must be an integer, got {value!r}"


def capacity_command(host: str, n: int) -> str:
    return f"cox host capacity {host} {n}"


def _run_set_host(profile_path: Path, key: str, value: str, dry_run: bool) -> int:
    """Edge. Writes through `cli._host_capacity`, the function `cox host capacity` calls."""
    parsed = parse_host_capacity(key, value)
    if isinstance(parsed, str):
        print(f"error: {parsed}")
        return 1
    host, n = parsed
    if dry_run:
        print(capacity_command(host, n))
        return 0
    from agent_tools.cli import _host_capacity  # cli imports this module, so a top-level import is circular

    return _host_capacity(argparse.Namespace(profile=str(profile_path), name=host, n=n, json=False))


def run_set(profile_path: Path, scope: str, key: str, value: str, dry_run: bool, commit: bool = False) -> int:
    """Edge. Plan the change against the target file's text and print the diff. Unless `dry_run`, write
    it; with `commit`, also commit it when it is a git-tracked cartridge. Profile files are machine-local
    and never committed. The `host` scope writes the hosts table instead of a file."""
    if scope == "host":
        return _run_set_host(profile_path, key, value, dry_run)
    profile_text = _read_text_or_none(profile_path)
    _, profile = parse_documents(None, profile_text)
    path = target_path(scope, profile_path, profile)
    if path is None:
        print(f"error: no file to edit for {scope}:{key}; the profile names no cartridge or the scope is unknown")
        return 1
    plan = settings_plan.plan_set(scope, key, value, _read_text_or_none(path) or "", _no_schema)
    if isinstance(plan, settings_plan.PlanError):
        print(f"error: {plan.reason}")
        return 1
    if plan.pat_only:
        print(f"Pat-only: {scope}:{key} is a Pat-only setting. Proceeding.")
    if not plan.diff:
        print(f"no change: {scope}:{key} already has this value in {path.name}")
        return 0
    print(plan.diff, end="")
    will_commit = commit and scope != "profile" and git_tracked(path)
    message = commit_message(key, value)
    if dry_run:
        if will_commit:
            print(commit_command(path.name, message))
        elif commit:
            print("no commit would be made (machine-local file)")
        return 0
    if will_commit and git_user_name(path.parent) is None:
        print("error: git config user.name is not set; nothing was written")
        return 1
    try:
        path.write_text(plan.new_text, encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {path}: {exc}")
        return 1
    if not will_commit:
        reason = "machine-local file" if commit else "--commit not given"
        print(f"wrote {path}; no commit was made ({reason})")
        return 0
    failure = git_commit(path, message)
    if failure is not None:
        print(f"error: wrote {path} but the commit failed: {failure}")
        return 1
    print(f"wrote {path} and committed it")
    return 0
