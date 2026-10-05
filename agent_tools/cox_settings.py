"""`cox settings get`: the settings rows of the cartridge and the profile, as text or JSON.

Parsing, grouping and formatting are pure functions of data. The edge, `git_tracked` and `run_get`,
only reads files, asks git and prints. The cartridge is `<cartridges_dir>/<team>/cartridge.yaml`,
both named by the profile, and its text is parsed by `chair_cap._load` (yaml.safe_load, `{}` on a
broken or non-mapping document).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_tools import route, settings_model
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
