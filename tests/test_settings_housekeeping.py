from agent_tools.settings_housekeeping import housekeeping_rows
from agent_tools.settings_model import SettingRow

PROFILE = {"team": "pat", "chair": {"housekeeping_hours": 12, "stale_days": 3}}
TRACKED = {"profile.yaml": True, "cartridge.yaml": True}


def test_rows_come_from_the_profile_chair_mapping():
    assert housekeeping_rows(PROFILE, TRACKED) == [
        SettingRow("housekeeping", "profile", "chair.housekeeping_hours", 12, "profile.yaml", True),
        SettingRow("housekeeping", "profile", "chair.stale_days", 3, "profile.yaml", True),
    ]


def test_absent_key_yields_no_row_and_no_default():
    profile = {"chair": {"stale_days": 5}}
    assert housekeeping_rows(profile, {}) == [
        SettingRow("housekeeping", "profile", "chair.stale_days", 5, "profile.yaml", False),
    ]


def test_no_chair_mapping_yields_no_rows():
    assert housekeeping_rows({"team": "pat"}, TRACKED) == []
    assert housekeeping_rows({"chair": None}, TRACKED) == []
