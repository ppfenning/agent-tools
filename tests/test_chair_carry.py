from agent_tools.chair_carry import checks_failed_stop, conflict_stop, plan_carry


def _row(task, seq, needs=(), commit=None):
    return {
        "task": task, "run": f"run{seq}", "host": "h", "branch": f"agents/run{seq}",
        "commit": commit or f"{task}{seq}", "needs": list(needs), "run_seq": seq,
    }


def _phase(approved, pending=()):
    return {"initiative": "init", "phase": "p1", "phase_branch": "init/p1", "approved": approved, "pending": list(pending)}


def test_stranded_phase_with_two_tasks_gives_one_carry():
    actions = plan_carry([_phase([_row("a", 1), _row("b", 1)])])
    assert [(a["kind"], a["pr_branch"], [p["task"] for p in a["picks"]]) for a in actions] == [
        ("carry_phase", "pr/carry-init-p1", ["a", "b"])
    ]


def test_task_in_two_runs_keeps_the_newest():
    (action,) = plan_carry([_phase([_row("a", 1), _row("a", 2)])])
    assert [(p["task"], p["run_seq"], p["commit"]) for p in action["picks"]] == [("a", 2, "a2")]


def test_split_phase_gives_one_action_with_both():
    actions = plan_carry([_phase([_row("a", 1), _row("b", 2)])])
    assert [[p["task"] for p in a["picks"]] for a in actions] == [["a", "b"]]


def test_pending_task_gives_nothing():
    assert plan_carry([_phase([_row("a", 1)], pending=["b"])]) == []


def test_dependent_task_follows_its_dependency():
    (action,) = plan_carry([_phase([_row("a", 1, needs=["b"]), _row("b", 1)])])
    assert [p["task"] for p in action["picks"]] == ["b", "a"]


def test_needs_cycle_gives_needs_chair():
    actions = plan_carry([_phase([_row("a", 1, needs=["b"]), _row("b", 1, needs=["a"])])])
    assert [a["kind"] for a in actions] == ["needs_chair"]
    assert "a, b" in actions[0]["reason"]


def test_conflict_stop_names_sorted_files():
    action = conflict_stop("init", "p1", "a", ["z.py", "m.py"])
    assert action["kind"] == "needs_chair"
    assert action["reason"] == (
        "initiative init phase p1: task a conflicts in m.py, z.py; the carry stopped and left the pr branch for the chair"
    )


def test_checks_failed_stop_names_failing_checks():
    action = checks_failed_stop("init", "p1", ["lint", "pytest"])
    assert action["kind"] == "needs_chair"
    assert "lint, pytest" in action["reason"]
