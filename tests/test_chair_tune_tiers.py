from agent_tools.chair_tune_tiers import TierRow, render_proposals, tier_proposals


def _current(role: str = "builder", rate: float = 85.0) -> TierRow:
    return TierRow(role, "high", "big", 40, rate, 10.0, f"roles.{role}.tier", current=True)


def _cheaper(role: str = "builder", rate: float = 80.0, runs: int = 12) -> TierRow:
    return TierRow(role, "low", "small", runs, rate, 4.0, f"roles.{role}.tier")


def test_cheaper_tier_4_9_points_lower_gives_proposal() -> None:
    assert len(tier_proposals([_current(), _cheaper(rate=80.1)])) == 1


def test_cheaper_tier_exactly_5_0_lower_gives_proposal() -> None:
    assert len(tier_proposals([_current(), _cheaper(rate=80.0)])) == 1


def test_cheaper_tier_5_1_lower_gives_none() -> None:
    assert tier_proposals([_current(), _cheaper(rate=79.9)]) == []


def test_cheaper_tier_with_higher_rate_gives_proposal() -> None:
    assert len(tier_proposals([_current(), _cheaper(rate=90.0)])) == 1


def test_cheaper_row_under_min_runs_gives_none() -> None:
    assert tier_proposals([_current(), _cheaper(runs=9)]) == []


def test_command_is_the_literal_settings_line() -> None:
    (proposal,) = tier_proposals([_current(), _cheaper()])
    assert proposal.command == "cox settings set roles.builder.tier low"


def test_two_roles_render_into_one_body() -> None:
    rows = [_current("builder"), _cheaper("builder"), _current("reviewer"), _cheaper("reviewer")]
    body = render_proposals(tier_proposals(rows))
    assert "cox settings set roles.builder.tier low" in body
    assert "cox settings set roles.reviewer.tier low" in body
    assert body.count("command:") == 2


def test_no_proposals_render_to_empty_string() -> None:
    assert render_proposals([]) == ""
