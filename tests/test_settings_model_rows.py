from agent_tools.settings_model_rows import model_tier_rows


def _row(key: str, value: str) -> dict:
    return {
        "section": "models and tiers",
        "scope": "cartridge",
        "key": key,
        "value": value,
        "source_file": "cartridge.yaml",
        "tracked": False,
        "pat_only": True,
    }


def test_rows_per_role_in_map_order():
    model_map = {"build": {"model": "opus", "tier": "3"}, "plan": {"model": "sonnet", "tier": "2"}}
    assert model_tier_rows(model_map) == [
        _row("models.build.model", "opus"),
        _row("models.build.tier", "3"),
        _row("models.plan.model", "sonnet"),
        _row("models.plan.tier", "2"),
    ]


def test_role_lacking_a_tier_yields_only_its_model_row():
    model_map = {"build": {"model": "opus", "tier": "3"}, "review": {"model": "haiku"}}
    assert model_tier_rows(model_map) == [
        _row("models.build.model", "opus"),
        _row("models.build.tier", "3"),
        _row("models.review.model", "haiku"),
    ]


def test_empty_map_yields_no_rows():
    assert model_tier_rows({}) == []


def test_no_plan_competition_min_tier_row():
    rows = model_tier_rows({"plan": {"model": "sonnet", "tier": "2"}})
    assert all(r["key"] != "policy.plan_competition.min_tier" for r in rows)
