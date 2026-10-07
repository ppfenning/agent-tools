"""Housekeeping rows for `chair.stale_days`, `chair.housekeeping_hours` and `analytics.snapshot_max_age_s`.

The chair reads the first two off the profile's `chair` mapping (`cli._chair_run_deps`), and the analytics
snapshot reads the third off `analytics`, never the cartridge or the store, so the true source_file of
each row is `profile.yaml`.
"""

from agent_tools.settings_model import PROFILE_FILE, SettingRow

SECTION = "housekeeping"
KEYS = ("chair.housekeeping_hours", "chair.stale_days", "analytics.snapshot_max_age_s")


def housekeeping_rows(profile: dict, tracked: dict[str, bool]) -> list[SettingRow]:
    """One row per key present under its profile mapping (`chair` or `analytics`); an absent key yields no row."""
    return [
        SettingRow(SECTION, "profile", key, section[name], PROFILE_FILE, tracked.get(PROFILE_FILE, False))
        for key in KEYS
        for group, _, name in (key.partition("."),)
        for section in (profile.get(group) if isinstance(profile.get(group), dict) else {},)
        if name in section
    ]
