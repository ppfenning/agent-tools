from agent_tools.route_guard import (
    DONE,
    REFUSED,
    InitiativeFound,
    NotFound,
    Ok,
    QueuedIntake,
    Refusal,
    initiative_guard,
    resolve_target,
)

ROWS = [
    {"kind": "intake", "initiative": "intake", "task_id": "add-widget", "state": "queued", "extra": {"id": "I-9"}},
    {"kind": "intake", "initiative": "intake", "task_id": "old-one", "state": "done", "extra": {}},
    {"kind": "task", "initiative": "widgets", "task_id": "w1", "state": "todo", "extra": {}},
]


def test_exit_codes_match_approve_and_decline():
    assert (DONE, REFUSED) == (0, 2)


def test_queued_intake_resolves_by_file_stem():
    assert resolve_target(ROWS, "add-widget") == QueuedIntake("add-widget")


def test_queued_intake_resolves_by_frontmatter_id():
    assert resolve_target(ROWS, "I-9") == QueuedIntake("add-widget")


def test_initiative_resolves_by_name():
    assert resolve_target(ROWS, "widgets") == InitiativeFound("widgets")


def test_unknown_id_is_not_found():
    assert resolve_target(ROWS, "nope") == NotFound("nope")


def test_done_intake_is_not_found():
    assert resolve_target(ROWS, "old-one") == NotFound("old-one")


def test_todo_and_ready_tasks_with_no_runs_pass():
    tasks = [{"task_id": "a", "state": "todo"}, {"task_id": "b", "state": "ready"}]
    assert initiative_guard(tasks, []) == Ok()


def test_live_run_refuses_with_the_run_id():
    refusal = initiative_guard([{"task_id": "a", "state": "todo"}], ["I412-7"])
    assert refusal == Refusal("live_run", "I412-7", "run I412-7 is live on this initiative; stop it first with: cox runs stop I412-7")


def test_task_past_ready_refuses_naming_the_task():
    tasks = [{"task_id": "a", "state": "ready"}, {"task_id": "b", "state": "in_progress"}]
    refusal = initiative_guard(tasks, [])
    assert (refusal.kind, refusal.subject) == ("task_past_ready", "b")
    assert "b" in refusal.reason


def test_live_run_is_named_before_a_task_past_ready():
    refusal = initiative_guard([{"task_id": "a", "state": "done"}], ["R1"])
    assert refusal.kind == "live_run"
