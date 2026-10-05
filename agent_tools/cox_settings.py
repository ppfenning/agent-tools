"""`cox settings get` and `cox settings set`: the settings rows of the cartridge and the profile.

Parsing, grouping and formatting are pure functions of data. The edge, `git_tracked`, `run_get`,
`git_user_name`, `git_commit` and `run_set`, only reads files, asks git, writes and prints. The cartridge is `<cartridges_dir>/<team>/cartridge.yaml`,
both named by the profile, and its text is parsed by `chair_cap._load` (yaml.safe_load, `{}` on a
broken or non-mapping document).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_tools import route, settings_model, settings_plan
from agent_tools.chair_cap import _load as _load_cartridge
from agent_tools.settings_model import SettingRow

Grouped = dict[str, list[SettingRow]]


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


def _row_json(row: SettingRow) -> dict:
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


def _row_line(row: SettingRow) -> str:
    where = "tracked" if row.tracked else "local"
    marker = "  [pat-only]" if pat_only_for(row) else ""
    return f"  {row.scope}:{row.key} = {json.dumps(row.value, default=str)}  ({row.source_file}, {where}){marker}"


def render_text(grouped: Grouped) -> str:
    """One block per non-empty section: its name, then one line per row; blocks are separated by a blank line."""
    blocks = [[name, *(_row_line(r) for r in rows)] for name, rows in grouped.items() if rows]
    return "\n\n".join("\n".join(block) for block in blocks)


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
    grouped = settings_model.rows(cartridge, profile, tracked)
    print(json.dumps(to_json(grouped), default=str, indent=2) if as_json else render_text(grouped))
    return 0


def commit_message(scope: str, key: str, value: str, author: str) -> str:
    """One line naming the setting, the raw value given, and the caller."""
    return f"settings: set {scope}:{key} = {value} (by {author})"


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


def run_set(profile_path: Path, scope: str, key: str, value: str, dry_run: bool) -> int:
    """Edge. Plan the change against the target file's text and print the diff. Unless `dry_run`, write
    it, and commit it as the caller when git tracks the file; a machine-local file is written only."""
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
    if dry_run:
        return 0
    tracked = git_tracked(path)
    author = git_user_name(path.parent) if tracked else None
    if tracked and author is None:
        print("error: git config user.name is not set; nothing was written")
        return 1
    try:
        path.write_text(plan.new_text, encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {path}: {exc}")
        return 1
    if author is None:
        print(f"wrote {path}; no commit was made (machine-local file)")
        return 0
    failure = git_commit(path, commit_message(scope, key, value, author))
    if failure is not None:
        print(f"error: wrote {path} but the commit failed: {failure}")
        return 1
    print(f"wrote {path} and committed it as {author}")
    return 0
