from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_types import Facts


def _task(
    id: str, repo: str = "r", needs: tuple[str, ...] = (), done: bool = True, run: str = "", fetch: bool = False
) -> dict:
    return {
        "id": id, "initiative": "i", "repo": repo, "phase_done": done, "needs": list(needs),
        "run": run or f"i-{id}", "needs_fetch": fetch,
    }


def _kinds(facts: Facts) -> list[str]:
    return [a["kind"] for a in plan_lands(facts)]


def test_two_tasks_on_one_unfetched_remote_run_plan_one_fetch_before_both_lands():
    assert _kinds(_facts([_task("a", run="i-1", fetch=True), _task("b", run="i-1", fetch=True)])) == ["fetch", "land", "land"]


def test_a_local_run_plans_a_land_only():
    assert _kinds(_facts([_task("a")])) == ["land"]


def _facts(approved: list[dict], landed: frozenset[str] = frozenset()) -> Facts:
    return {
        "lease": {"holder": "a", "host": "h", "epoch": 3, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.4,
            "hard_stop_fraction": 0.9,
            "launch_cap": 2,
            "go_degraded": False,
        },
        "dispatch": {"max_in_flight": 3, "live_runs": 0},
        "approved": approved,
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [], "landed": set(landed)}],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": False,
    }


def _ids(facts: Facts) -> list[str]:
    return [a["task_id"] for a in plan_lands(facts)]


def test_a_done_phase_task_is_planned_and_a_not_done_one_gets_none():
    assert _ids(_facts([_task("a"), _task("b", done=False)])) == ["a"]


def test_a_task_comes_after_the_task_it_needs_whatever_the_input_order():
    assert _ids(_facts([_task("b", needs=("a",)), _task("a")])) == ["a", "b"]


def test_lands_for_one_repository_are_contiguous():
    assert _ids(_facts([_task("a1", "A"), _task("b1", "B"), _task("a2", "A")])) == ["a1", "a2", "b1"]


def test_a_cross_repository_need_puts_the_needed_repository_first():
    assert _ids(_facts([_task("a1", "A", needs=("b1",)), _task("b1", "B")])) == ["b1", "a1"]


def test_a_need_that_is_not_landed_and_not_planned_leaves_the_task_and_its_dependents_out():
    approved = [_task("a", needs=("x",)), _task("b", needs=("a",)), _task("c", done=False), _task("d", needs=("c",))]
    assert _ids(_facts(approved)) == []


def test_a_landed_need_lets_the_task_through():
    assert _ids(_facts([_task("a", needs=("x",))], landed=frozenset({"x"}))) == ["a"]


def test_a_cycle_and_an_unknown_id_are_omitted_without_raising():
    approved = [
        _task("a", needs=("b",)),
        _task("b", needs=("a",)),
        _task("c", needs=("a",)),
        _task("d"),
        _task("e", needs=("nope",)),
    ]
    assert _ids(_facts(approved)) == ["d"]


def test_a_chain_that_crosses_back_into_a_repository_plans_its_prefix_and_the_rest_next_tick():
    approved = [_task("a1", "A"), _task("b1", "B", needs=("a1",)), _task("a2", "A", needs=("b1",)), _task("c", "C")]
    assert _ids(_facts(approved)) == ["a1", "b1", "c"]
    assert _ids(_facts([_task("a2", "A", needs=("b1",))], landed=frozenset({"a1", "b1"}))) == ["a2"]


def test_a_task_in_another_repository_that_needs_only_the_planned_prefix_is_planned():
    approved = [_task("a1", "A"), _task("b1", "B", needs=("a1",)), _task("a2", "A", needs=("b1",)), _task("d1", "D", needs=("a1",))]
    assert _ids(_facts(approved)) == ["a1", "b1", "d1"]


def test_a_task_needing_one_left_for_a_later_tick_is_left_too():
    approved = [_task("a1", "A"), _task("b1", "B", needs=("a1",)), _task("a2", "A", needs=("b1",)), _task("e", "E", needs=("a2",))]
    assert _ids(_facts(approved)) == ["a1", "b1"]


def test_a_long_chain_plans_in_order_without_raising():
    approved = [_task("t0")] + [_task(f"t{n}", needs=(f"t{n - 1}",)) for n in range(1, 5000)]
    assert _ids(_facts(approved)) == [f"t{n}" for n in range(5000)]


def test_a_repeated_id_keeps_its_first_occurrence_position_and_fields():
    approved = [_task("x", "A"), _task("y", "A"), _task("x", "B")]
    assert [(a["task_id"], a["repo"]) for a in plan_lands(_facts(approved))] == [("x", "A"), ("y", "A")]


def test_every_action_is_a_land_with_no_epoch_yet():
    assert plan_lands(_facts([_task("a", "R")])) == [
        {"kind": "land", "task_id": "a", "repo": "R", "run": "i-a", "initiative": "i", "epoch": None}
    ]
