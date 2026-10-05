"""Housekeeping rows for `chair.stale_days` and `chair.housekeeping_hours`.

The chair reads both off the profile's `chair` mapping (`cli._chair_run_deps`), never the cartridge or
the store, so the true source_file of each row is `profile.yaml`.
"""

from agent_tools.settings_model import PROFILE_FILE, SettingRow

SECTION = "housekeeping"
KEYS = ("chair.housekeeping_hours", "chair.stale_days")


def housekeeping_rows(profile: dict, tracked: dict[str, bool]) -> list[SettingRow]:
    """One row per key present under `profile["chair"]`; an absent key yields no row and no default."""
    chair = profile.get("chair")
    section = chair if isinstance(chair, dict) else {}
    return [
        SettingRow(SECTION, "profile", key, section[name], PROFILE_FILE, tracked.get(PROFILE_FILE, False))
        for key in KEYS
        for name in (key.removeprefix("chair."),)
        if name in section
    ]
