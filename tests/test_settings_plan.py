from agent_tools import chair_cap
from agent_tools.settings_plan import _CHAIR_READS, Plan, PlanError, plan_set

OLD = "policy:\n  dispatch:\n    max_in_flight: 2\n"
NEW = "policy:\n  dispatch:\n    max_in_flight: 4\n"
DIFF = (
    "--- a/cartridge.yaml\n"
    "+++ b/cartridge.yaml\n"
    "@@ -1,3 +1,3 @@\n"
    " policy:\n"
    "   dispatch:\n"
    "-    max_in_flight: 2\n"
    "+    max_in_flight: 4\n"
)


def _valid(text: str) -> list[str]:
    return []


def _rejects(text: str) -> list[str]:
    return ["max_in_flight must be at most 3"]


def test_valid_change_returns_new_text_and_diff():
    assert plan_set("cartridge", "policy.dispatch.max_in_flight", "4", OLD, _valid) == Plan(NEW, DIFF, False)


def test_schema_violation_returns_error_with_no_new_text():
    result = plan_set("cartridge", "policy.dispatch.max_in_flight", "4", OLD, _rejects)
    assert result == PlanError("cartridge.yaml violates the schema: max_in_flight must be at most 3")
    assert not hasattr(result, "new_text")


def test_unknown_key_is_an_error():
    assert plan_set("cartridge", "policy.nope", "1", OLD, _valid) == PlanError("unknown setting cartridge:policy.nope")


def test_value_that_fails_coercion_is_an_error():
    result = plan_set("cartridge", "policy.dispatch.max_in_flight", "abc", OLD, _valid)
    assert result == PlanError("cartridge:policy.dispatch.max_in_flight: expected int, got 'abc'")


PROFILE = (
    "team: pat\n"
    "skills_roots: [a, b]\n"
    'sources: {"gh": {"repo": "o/r"}}\n'
    'repo_map: {"r": "/x"}\n'
    "spend:\n"
    "  window_ceiling_usd: 50.0\n"
    "  weekly_reset: Sun 04:00 America/New_York\n"
    "chair:\n"
    "  stale_days: 3.0\n"
    "lane_hosts:\n"
    "  - name: web\n"
    "    capacity: 2\n"
)


def test_chair_profile_parser_rejection_is_its_message():
    result = plan_set("profile", "window_ceiling_usd", "abc", "team: pat\n")
    assert result == PlanError("line 3:   window_ceiling_usd: abc")


def test_chair_cartridge_reader_rejection_names_the_rule():
    result = plan_set("cartridge", "policy.dispatch.max_in_flight", "0", OLD, _valid)
    assert result == PlanError("policy.dispatch.max_in_flight: the chair ignores this value, expected a positive integer")


def test_profile_set_keeps_the_chair_format_for_every_other_key():
    result = plan_set("profile", "team", "other", PROFILE)
    assert isinstance(result, Plan) and result.new_text == PROFILE.replace("team: pat", "team: other")


def test_profile_spend_key_is_written_under_spend():
    result = plan_set("profile", "window_ceiling_usd", "5", "team: pat\n")
    assert isinstance(result, Plan) and result.new_text == "team: pat\nspend:\n  window_ceiling_usd: 5\n"


def test_chair_reads_covers_every_chair_cap_reader():
    readers = {reader for reader, _ in _CHAIR_READS.values()}
    assert readers == {getattr(chair_cap, n) for n in dir(chair_cap) if n.endswith("_from_cartridge")}


def test_pat_only_flag_is_carried():
    result = plan_set("cartridge", "policy.review_tier", "3", "policy:\n  review_tier: 2\n", _valid)
    assert isinstance(result, Plan) and result.pat_only is True
