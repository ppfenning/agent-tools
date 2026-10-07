from agent_tools.chair_facts import DEFAULT_HOUSEKEEPING_HOURS, DEFAULT_STALE_DAYS
from agent_tools.settings_model import SettingRow
from agent_tools.settings_sections.defaults import DEFAULT_KEYS, default_rows


def _set(scope: str, key: str) -> dict:
    return {
        "section": "x",
        "scope": scope,
        "key": key,
        "value": 1,
        "source_file": "cartridge.yaml",
        "tracked": True,
        "pat_only": False,
    }


def _by_key(rows: list[dict]) -> dict:
    return {r["key"]: r for r in rows}


def test_missing_keys_yield_exactly_those_built_in_rows():
    present = [_set("cartridge", "epic_threshold"), _set("profile", "chair.stale_days"), _set("profile", "team")]
    rows = default_rows(present)
    assert [r["key"] for r in rows] == [
        "policy.build_budget_usd_max",
        "policy.review_tier",
        "chair.housekeeping_hours",
        "analytics.snapshot_max_age_s",
    ]
    assert {r["source_file"] for r in rows} == {"built-in"}
    assert {r["tracked"] for r in rows} == {False}
    assert _by_key(rows)["chair.housekeeping_hours"]["section"] == "housekeeping"


def test_a_set_key_gets_no_default_row():
    rows = default_rows([_set("profile", "chair.stale_days")])
    assert "chair.stale_days" not in _by_key(rows)
    assert len(rows) == len(DEFAULT_KEYS) - 1


def test_scope_is_part_of_the_identity():
    rows = default_rows([_set("cartridge", "chair.stale_days")])
    assert "chair.stale_days" in _by_key(rows)


def test_setting_rows_count_as_set():
    row = SettingRow("housekeeping", "profile", "chair.stale_days", 3, "profile.yaml", False)
    assert "chair.stale_days" not in _by_key(default_rows([row]))


def test_every_key_set_yields_no_rows():
    assert default_rows([_set(k.scope, k.key) for k in DEFAULT_KEYS]) == []


def test_code_defaults_are_imported_and_cartridge_keys_are_null():
    by_key = _by_key(default_rows([]))
    assert by_key["chair.stale_days"]["value"] == DEFAULT_STALE_DAYS
    assert by_key["chair.housekeeping_hours"]["value"] == DEFAULT_HOUSEKEEPING_HOURS
    assert "note" not in by_key["chair.stale_days"]
    assert by_key["epic_threshold"]["value"] is None
    assert by_key["epic_threshold"]["note"] == "set by the cartridge"


def test_models_and_tiers_default_rows_are_pat_only():
    by_key = _by_key(default_rows([]))
    assert by_key["policy.review_tier"]["section"] == "models and tiers"
    assert by_key["policy.review_tier"]["pat_only"] is True
    assert by_key["chair.stale_days"]["pat_only"] is False


def test_input_rows_are_not_mutated():
    present = [_set("profile", "team")]
    before = [dict(r) for r in present]
    default_rows(present)
    assert present == before
