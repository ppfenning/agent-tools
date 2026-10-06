from datetime import UTC, datetime, timedelta

from agent_tools.chair_plan_tune import plan_tune

_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_OVER = {"weekly_fraction_used": 0.8, "week_elapsed_fraction": 0.5}  # ratio 1.6
_CURRENT = {
    "role": "builder", "tier": "high", "model": "big", "runs": 40,
    "approval_pct": 90.0, "cost_per_approved": 10.0, "settings_key": "roles.builder.tier", "current": True,
}  # fmt: skip
_CHEAPER = {
    **_CURRENT,
    "tier": "low",
    "model": "small",
    "runs": 12,
    "approval_pct": 85.0,
    "cost_per_approved": 4.0,
    "current": False,
}
_TOO_LOSSY = {**_CHEAPER, "approval_pct": 70.0}


def _facts(hosts=None, stats=(), last_tuned_at=None, meter=_OVER) -> dict:
    tuning = {"stats": list(stats), "meter": meter, "hosts": hosts or {}, "last_tuned_at": last_tuned_at}
    return {"tuning": tuning}


def _host(lanes: int, min_lanes: int = 1) -> dict:
    return {"lanes": lanes, "min_lanes": min_lanes, "max_lanes": 8}


def test_a_changed_host_gives_one_tune_lanes_with_from_to_and_reason():
    [action] = plan_tune(_facts(hosts={"a": _host(4)}), _NOW)  # type: ignore[arg-type]
    assert (action["kind"], action["host"], action["from_lanes"], action["to_lanes"]) == ("tune_lanes", "a", 4, 3)
    assert "lowering lanes" in action["reason"]


def test_two_changed_hosts_give_two_actions():
    actions = plan_tune(_facts(hosts={"a": _host(4), "b": _host(4)}), _NOW)  # type: ignore[arg-type]
    assert [a["host"] for a in actions] == ["a", "b"]


def test_an_unchanged_host_gives_none():
    assert plan_tune(_facts(hosts={"a": _host(1)}), _NOW) == []  # type: ignore[arg-type]


def test_a_proposal_worthy_role_gives_exactly_one_propose_tiers():
    [action] = plan_tune(_facts(stats=[_CURRENT, _CHEAPER]), _NOW)  # type: ignore[arg-type]
    assert action["kind"] == "propose_tiers"
    assert [p["role"] for p in action["proposals"]] == ["builder"]
    assert "cox settings set roles.builder.tier low" in action["body"]


def test_no_proposal_worthy_role_gives_none():
    assert plan_tune(_facts(stats=[_CURRENT, _TOO_LOSSY]), _NOW) == []  # type: ignore[arg-type]


def test_last_tuned_23_hours_ago_gives_nothing():
    facts = _facts(
        hosts={"a": _host(4)}, stats=[_CURRENT, _CHEAPER], last_tuned_at=(_NOW - timedelta(hours=23)).isoformat()
    )
    assert plan_tune(facts, _NOW) == []  # type: ignore[arg-type]


def test_last_tuned_25_hours_ago_acts():
    facts = _facts(
        hosts={"a": _host(4)}, stats=[_CURRENT, _CHEAPER], last_tuned_at=(_NOW - timedelta(hours=25)).isoformat()
    )
    assert [a["kind"] for a in plan_tune(facts, _NOW)] == ["tune_lanes", "propose_tiers"]  # type: ignore[arg-type]


def test_an_empty_tuning_entry_gives_nothing():
    assert plan_tune({"tuning": {}}, _NOW) == []  # type: ignore[arg-type]
    assert plan_tune({}, _NOW) == []  # type: ignore[arg-type]


def test_every_returned_kind_is_tune_lanes_or_propose_tiers():
    actions = plan_tune(_facts(hosts={"a": _host(4)}, stats=[_CURRENT, _CHEAPER]), _NOW)  # type: ignore[arg-type]
    assert {a["kind"] for a in actions} == {"tune_lanes", "propose_tiers"}
