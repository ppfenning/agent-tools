"""Lanes-and-machines rows as plain dicts; `SettingRow` has no `pat_only`, and the verb prints seven fields."""

from __future__ import annotations

# Private names, imported the way settings_model.py imports them.
from agent_tools.editor_model import _MISSING, _get
from agent_tools.settings_model import setting_for

SECTION = "lanes and machines"
HOSTS_SOURCE = "store: hosts"
LOCAL_LANES = setting_for("cartridge", "policy.dispatch.local_lanes")


def _pat_only(scope: str, key: str) -> bool:
    setting = setting_for(scope, key)
    return setting.pat_only if setting is not None else False


def _row(scope: str, key: str, value: object, source_file: str, tracked: bool) -> dict:
    return {"section": SECTION, "scope": scope, "key": key, "value": value,
            "source_file": source_file, "tracked": tracked, "pat_only": _pat_only(scope, key)}


def host_rows(hosts: dict[str, object], profile: dict, tracked: dict[str, bool]) -> list[dict]:
    """Host rows sorted by name, then local_lanes under its registry identity when `profile` holds it."""
    local = _get(profile, LOCAL_LANES.path)
    lanes = LOCAL_LANES.scope, LOCAL_LANES.key, local, LOCAL_LANES.source_file, tracked.get(LOCAL_LANES.source_file, False)
    return [
        *(_row("host", f"{host}.capacity", hosts[host], HOSTS_SOURCE, False) for host in sorted(hosts)),
        *([] if local is _MISSING else [_row(*lanes)]),
    ]
