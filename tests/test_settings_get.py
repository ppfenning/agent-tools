from agent_tools import cox_settings

CARTRIDGE = {"policy": {"build_budget_usd_max": 5}}
PROFILE = {"chair": {"stale_days": 3}}
SECTIONS = ("builds and budgets", "housekeeping", "models and tiers")


def _grouped() -> dict:
    base = cox_settings.sections(CARTRIDGE, PROFILE, {}, {"cartridge.yaml": True, "profile.yaml": False})
    return cox_settings.with_defaults(base)


def _rows(grouped: dict) -> dict:
    return {s["name"]: s["rows"] for s in cox_settings.to_json(grouped)["sections"]}


def _keys(rows: list[dict]) -> list[tuple[str, str]]:
    return [(r["scope"], r["key"]) for r in rows]


def test_defaults_follow_the_set_rows_in_each_section():
    by_section = _rows(_grouped())
    assert _keys(by_section["builds and budgets"]) == [
        ("cartridge", "policy.build_budget_usd_max"), ("cartridge", "epic_threshold"),
    ]
    assert _keys(by_section["housekeeping"]) == [
        ("profile", "chair.stale_days"), ("profile", "chair.housekeeping_hours"),
    ]
    assert _keys(by_section["models and tiers"]) == [("cartridge", "policy.review_tier")]


def test_set_rows_keep_their_source_and_defaults_say_built_in():
    by_section = _rows(_grouped())
    assert by_section["builds and budgets"][0]["source_file"] == "cartridge.yaml"
    assert by_section["builds and budgets"][0]["value"] == 5
    assert by_section["housekeeping"][0]["source_file"] == "profile.yaml"
    defaults = [r for s in SECTIONS for r in by_section[s] if r["source_file"] == "built-in"]
    assert [r["key"] for r in defaults] == ["epic_threshold", "chair.housekeeping_hours", "policy.review_tier"]


def test_a_set_key_never_also_appears_as_a_default():
    by_section = _rows(_grouped())
    for name in SECTIONS:
        keys = _keys(by_section[name])
        assert len(set(keys)) == len(keys)


def test_empty_settings_fill_the_three_sections_with_defaults():
    by_section = _rows(cox_settings.with_defaults(cox_settings.sections({}, {}, {}, {})))
    assert all(by_section[name] for name in SECTIONS)
    assert {r["source_file"] for name in SECTIONS for r in by_section[name]} == {"built-in"}


def test_text_marks_a_default_row_built_in():
    text = cox_settings.render_text(_grouped())
    assert "profile:chair.housekeeping_hours = " in text
    line = next(ln for ln in text.splitlines() if "chair.housekeeping_hours" in ln)
    assert "(built-in, local)" in line
    assert "(cartridge.yaml, tracked)" in next(ln for ln in text.splitlines() if "build_budget_usd_max" in ln)


def test_with_defaults_does_not_mutate_its_input():
    base = cox_settings.sections(CARTRIDGE, PROFILE, {}, {})
    before = {name: list(rows) for name, rows in base.items()}
    cox_settings.with_defaults(base)
    assert base == before
