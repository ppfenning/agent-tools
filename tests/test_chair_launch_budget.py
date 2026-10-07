from datetime import UTC, datetime, timedelta

from agent_tools.chair_launch_budget import Launch, RunOutcome, launch_budget

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _launches(*minutes_ago: int) -> list[Launch]:
    return [Launch(NOW - timedelta(minutes=m), "launch_epic") for m in minutes_ago]


def _run(run_id: str, reason: str = "dirty tree", body: str = "b1", head: str = "h1") -> RunOutcome:
    return RunOutcome(run_id, True, reason, body, head)


def _call(kind="launch_epic", launches=(), outcomes=(), body="b1", head="h1", limit=4):
    return launch_budget(NOW, kind, launches, outcomes, body, head, limit)


def test_third_launch_allowed_at_limit_four():
    assert _call(launches=_launches(5, 10)).allowed


def test_fifth_launch_refused_with_budget_reason():
    verdict = _call(launches=_launches(5, 10, 20, 30))
    assert not verdict.allowed
    assert verdict.kind == "budget"
    assert "max_launches_per_hour=4" in verdict.reason


def test_launches_older_than_an_hour_do_not_count():
    assert _call(launches=_launches(61, 90, 120, 300)).allowed


def test_two_same_reason_quarantines_refuse_relaunch():
    verdict = _call("relaunch", outcomes=[_run("r2", "Dirty  Tree"), _run("r1")])
    assert not verdict.allowed
    assert verdict.kind == "relaunch_loop"
    assert verdict.run_ids == ("r2", "r1")


def test_changed_body_allows_relaunch():
    assert _call("relaunch", outcomes=[_run("r2"), _run("r1")], body="b2").allowed


def test_changed_main_head_allows_relaunch():
    assert _call("relaunch", outcomes=[_run("r2"), _run("r1")], head="h2").allowed


def test_changed_reason_allows_relaunch():
    assert _call("relaunch", outcomes=[_run("r2", "other"), _run("r1")]).allowed


def test_one_quarantine_allows_relaunch():
    assert _call("relaunch", outcomes=[_run("r1")]).allowed


def test_launch_epic_allowed_while_relaunch_refused():
    outcomes = [_run("r2"), _run("r1")]
    assert not _call("relaunch", outcomes=outcomes).allowed
    assert _call("launch_epic", outcomes=outcomes).allowed
