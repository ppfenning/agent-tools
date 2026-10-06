from agent_tools.chair_decompose_runs import parse_run, select_runs
from agent_tools.chair_decompose_streak import DecomposeRun

REFUSAL = "chair: plan approved but not executed: launch_decompose"


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
