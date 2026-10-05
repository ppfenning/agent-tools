from agent_tools.settings_builds import BUILDS_KEYS, builds_rows
from agent_tools.settings_model import SettingRow

FILE = "cartridge.yaml"
TRACKED = {FILE: True}

FULL = {
    "policy": {"build_budget_usd_max": 2.5},
    "epic_threshold": 3,
    "landing_areas": {"checks": ["lint", "tests"]},
}


def _row(key, value, tracked=True):
    return SettingRow("builds and budgets", "cartridge", key, value, FILE, tracked)


def test_keys_have_no_duplicates():
    assert len(set(BUILDS_KEYS)) == len(BUILDS_KEYS)


def test_full_cartridge_yields_exact_rows_in_order():
    assert builds_rows(FULL, FILE, TRACKED) == [
        _row("policy.build_budget_usd_max", 2.5),
        _row("epic_threshold", 3),
        _row("landing_areas.checks", ["lint", "tests"]),
    ]


def test_missing_keys_yield_no_rows_and_no_defaults():
    cartridge = {"policy": {"review_tier": "2"}, "epic_threshold": 4}
    assert builds_rows(cartridge, FILE, TRACKED) == [_row("epic_threshold", 4)]


def test_empty_cartridge_yields_no_rows():
    assert builds_rows({}, FILE, TRACKED) == []


def test_source_file_is_the_name_the_caller_passes():
    rows = builds_rows({"epic_threshold": 3}, "other.yaml", {"other.yaml": True})
    assert rows == [SettingRow("builds and budgets", "cartridge", "epic_threshold", 3, "other.yaml", True)]


def test_file_missing_from_tracked_map_reads_as_machine_local():
    assert builds_rows({"epic_threshold": 3}, FILE, {}) == [_row("epic_threshold", 3, tracked=False)]
