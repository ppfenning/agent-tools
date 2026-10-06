from agent_tools.settings_model import REGISTRY, SECTIONS, SettingRow, rows, setting_for
from agent_tools.settings_sections.defaults import DEFAULT_KEYS

TRACKED = {"cartridge.yaml": True, "profile.yaml": False}


def _row(section, scope, key, value, tracked):
    file = "cartridge.yaml" if scope == "cartridge" else "profile.yaml"
    return SettingRow(section, scope, key, value, file, tracked)


def test_sections_come_back_in_order_even_when_empty():
    assert list(rows({}, {}, TRACKED)) == list(SECTIONS)
    assert all(group == [] for group in rows({}, {}, TRACKED).values())


def test_lanes_and_machines():
    cartridge = {"policy": {"dispatch": {"max_in_flight": 4, "local_lanes": "any"}}}
    assert rows(cartridge, {}, TRACKED)["lanes and machines"] == [
        _row("lanes and machines", "cartridge", "policy.dispatch.max_in_flight", 4, True),
        _row("lanes and machines", "cartridge", "policy.dispatch.local_lanes", "any", True),
    ]


def test_spend_and_pacing():
    cartridge = {"policy": {"pacing": {"hard_stop_fraction": 0.95}}}
    profile = {"window_ceiling_usd": 40.0, "weekly_reset": "Sun 04:00 UTC"}
    assert rows(cartridge, profile, TRACKED)["spend and pacing"] == [
        _row("spend and pacing", "profile", "window_ceiling_usd", 40.0, False),
        _row("spend and pacing", "profile", "weekly_reset", "Sun 04:00 UTC", False),
        _row("spend and pacing", "cartridge", "policy.pacing.hard_stop_fraction", 0.95, True),
    ]


def test_builds_and_budgets():
    cartridge = {"policy": {"build_budget_usd_max": 2.5}}
    assert rows(cartridge, {}, TRACKED)["builds and budgets"] == [
        _row("builds and budgets", "cartridge", "policy.build_budget_usd_max", 2.5, True),
    ]


def test_models_and_tiers():
    cartridge = {"policy": {"review_tier": "2", "plan_competition": {"min_tier": "1"}}}
    assert rows(cartridge, {}, TRACKED)["models and tiers"] == [
        _row("models and tiers", "cartridge", "policy.review_tier", "2", True),
        _row("models and tiers", "cartridge", "policy.plan_competition.min_tier", "1", True),
    ]


def test_crew_seats():
    cartridge = {"crew": {"builder": {"enabled": True, "skills": ["build"]}, "scout": "off"}}
    assert rows(cartridge, {}, TRACKED)["crew seats"] == [
        _row("crew seats", "cartridge", "crew.builder.enabled", True, True),
        _row("crew seats", "cartridge", "crew.builder.skills", ["build"], True),
        _row("crew seats", "cartridge", "crew.scout", "off", True),
    ]


def test_housekeeping():
    profile = {"log_retention_days": 14, "chair": {"stale_days": 3.0}}
    assert rows({}, profile, TRACKED)["housekeeping"] == [
        _row("housekeeping", "profile", "log_retention_days", 14, False),
        _row("housekeeping", "profile", "chair.stale_days", 3.0, False),
    ]


def test_profile_files_and_tracked_flag_follows_the_map():
    profile = {"team": "acme", "assume": "a"}
    expected = [
        _row("profile files", "profile", "team", "acme", True),
        _row("profile files", "profile", "assume", "a", True),
    ]
    assert rows({}, profile, {"profile.yaml": True})["profile files"] == expected
    assert [r.tracked for r in rows({}, profile, {})["profile files"]] == [False, False]


def test_every_models_and_tiers_setting_is_pat_only_and_nothing_else_is():
    flagged = {s.key for s in REGISTRY if s.pat_only}
    assert flagged == {s.key for s in REGISTRY if s.section == "models and tiers"}
    assert flagged == {
        "policy.review_tier",
        "policy.plan_competition.min_tier",
        "policy.pacing.tier_ladder",
        "policy.pacing.effort_ladder",
    }


def test_setting_for_resolves_registry_and_crew_keys():
    assert setting_for("cartridge", "policy.review_tier").section == "models and tiers"
    assert setting_for("profile", "team").source_file == "profile.yaml"
    assert setting_for("cartridge", "crew.builder.enabled").section == "crew seats"
    assert setting_for("cartridge", "nope") is None
    assert setting_for("profile", "policy.review_tier") is None


def test_every_defaults_table_key_resolves_in_the_registry():
    found = [(spec, setting_for(spec.scope, spec.key)) for spec in DEFAULT_KEYS]
    assert all(setting is not None for _, setting in found)
    assert [(s.section, s.pat_only) for _, s in found] == [(spec.section, spec.pat_only) for spec, _ in found]
