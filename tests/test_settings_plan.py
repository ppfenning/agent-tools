from agent_tools.settings_plan import Plan, PlanError, plan_set

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


def test_pat_only_flag_is_carried():
    result = plan_set("cartridge", "policy.review_tier", "3", "policy:\n  review_tier: 2\n", _valid)
    assert isinstance(result, Plan) and result.pat_only is True
