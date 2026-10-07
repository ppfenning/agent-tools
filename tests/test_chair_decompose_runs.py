from agent_tools.chair_decompose_runs import parse_run, select_runs
from agent_tools.chair_decompose_streak import DecomposeRun, at_limit, streak

REFUSAL = "chair: plan approved but not executed: launch_decompose"
LINT = "initiative-decompose failed: t1: reach — touches a file outside the surfaces"


def _recorded(run_id: str, proposals: int) -> str:
    return f"start\nrecorded {run_id}: 1 auto-applied, 0 gated decision(s), {proposals} proposal(s)\n"


def _run(run_id: str, log: str, items: int | None = 5) -> DecomposeRun:
    return parse_run({"run_id": run_id, "log": log, "task_items": items})


def test_run_with_created_items_wrote_items():
    record = {"run_id": "idea-1", "log": "start\n", "task_items": 3}
    assert parse_run(record) == DecomposeRun("idea-1", True, None)


def test_run_with_none_and_a_refusal_line():
    record = {"run_id": "idea-2", "log": f"start\n{REFUSAL}\nlater approved but not executed\n", "task_items": 0}
    assert parse_run(record) == DecomposeRun("idea-2", False, REFUSAL)


def test_run_with_none_and_no_refusal_line():
    assert parse_run({"run_id": "idea-3", "log": "done\n", "task_items": 0}) == DecomposeRun("idea-3", False, None)


def test_unknown_task_items_read_as_wrote_items():
    assert parse_run({"run_id": "idea-5", "log": None, "task_items": None}) == DecomposeRun("idea-5", True, None)


def test_malformed_records_read_as_wrote_items():
    assert parse_run({"run_id": "idea-4", "log": 7, "task_items": 0}) == DecomposeRun("idea-4", True, None)
    assert parse_run({"run_id": "idea-6", "log": "", "task_items": True}) == DecomposeRun("idea-6", True, None)
    assert parse_run({"log": "", "task_items": 0}) == DecomposeRun("", True, None)
    assert parse_run(None) == DecomposeRun("", True, None)


def test_select_runs_keeps_the_intake_and_orders_oldest_first():
    rows = [
        {"run_id": "a-2", "launched_at": "2026-10-02"},
        {"run_id": "b-1", "launched_at": "2026-10-01"},
        {"run_id": "a-1", "launched_at": "2026-10-01"},
        {"run_id": "a-10-1", "launched_at": "2026-10-03"},
    ]
    assert [r["run_id"] for r in select_runs(rows, "a")] == ["a-1", "a-2"]


def test_lint_refused_runs_reach_the_limit_though_the_initiative_holds_items():
    runs = [_run("idea-1", f"start\n{LINT}\n"), _run("idea-2", f"start\n{LINT}\n")]
    assert [r.wrote_items for r in runs] == [False, False]
    s = streak(runs)
    assert at_limit(s)
    assert s.first_refusal == LINT


def test_a_refused_run_after_a_writing_run_still_counts_as_empty():
    runs = [_run("idea-1", _recorded("idea-1", 3)), _run("idea-2", f"{LINT}\n")]
    assert [r.wrote_items for r in runs] == [True, False]
    assert streak(runs).empty_count == 1


def test_a_writing_run_resets_the_streak():
    runs = [_run("idea-1", f"{LINT}\n"), _run("idea-2", _recorded("idea-2", 2)), _run("idea-3", f"{LINT}\n")]
    s = streak(runs)
    assert not at_limit(s)
    assert s.run_ids == ("idea-3",)


def test_a_recorded_line_with_zero_proposals_is_empty():
    assert _run("idea-4", _recorded("idea-4", 0)) == DecomposeRun("idea-4", False, None)


def test_a_recorded_line_for_another_run_does_not_count():
    assert _run("idea-5", _recorded("idea-9", 4), items=0) == DecomposeRun("idea-5", False, None)


def test_a_log_with_neither_line_falls_back_to_the_store_count():
    assert _run("idea-6", "start\n", items=4).wrote_items is True
    assert _run("idea-7", "start\n", items=0).wrote_items is False


def test_approved_but_not_executed_still_yields_its_refusal_line():
    assert _run("idea-8", f"start\n{REFUSAL}\n", items=0) == DecomposeRun("idea-8", False, REFUSAL)
