from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_types import Facts


def _init(id: str, started: bool = True, ready: tuple[str, ...] = ("t1",)) -> dict:
    return {
        "id": id,
        "started": started,
        "ready_tasks": [{"id": t, "needs": []} for t in ready],
        "landed": set(),
    }


def _facts(**over) -> Facts:
    base = {
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.0,
            "hard_stop_fraction": 1.0,
            "launch_cap": 4,
            "go_degraded": False,
        },
        "dispatch": {"max_in_flight": 4, "live_runs": 0},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
    }
    return {**base, **over}


def test_no_free_lanes_returns_an_empty_list():
    facts = _facts(initiatives=[_init("a")], intake=["i1", "i2"], sources_configured=True)
    assert plan_fill(facts, 0) == []
    assert plan_fill(facts, -1) == []


def test_started_initiatives_launch_one_epic_each_up_to_free_lanes():
    facts = _facts(initiatives=[_init("a"), _init("b")], work_store_ready=True)
    assert plan_fill(facts, 1) == [{"kind": "launch_epic", "initiative": "a"}]
    assert plan_fill(facts, 3) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_epic", "initiative": "b"},
    ]


def test_an_initiative_launches_one_epic_however_many_tasks_are_ready():
    facts = _facts(initiatives=[_init("a", ready=("t1", "t2", "t3"))], work_store_ready=True)
    assert plan_fill(facts, 3) == [{"kind": "launch_epic", "initiative": "a"}]


def test_a_taskless_initiative_launches_no_epic():
    assert plan_fill(_facts(initiatives=[_init("a", ready=()), _init("b", started=False, ready=())]), 3) == []


def test_an_unstarted_initiative_with_a_ready_task_launches_when_a_lane_is_free():
    facts = _facts(initiatives=[_init("a", started=False)], work_store_ready=True)
    assert plan_fill(facts, 1) == [{"kind": "launch_epic", "initiative": "a"}]


def test_started_initiatives_launch_before_unstarted_ones_each_in_docket_order():
    facts = _facts(initiatives=[_init("u1", started=False), _init("s1"), _init("u2", started=False), _init("s2")])
    assert plan_fill(facts, 1) == [{"kind": "launch_epic", "initiative": "s1"}]
    assert plan_fill(facts, 3) == [
        {"kind": "launch_epic", "initiative": "s1"},
        {"kind": "launch_epic", "initiative": "s2"},
        {"kind": "launch_epic", "initiative": "u1"},
    ]


def test_a_withheld_initiative_launches_no_epic_but_the_lane_goes_to_the_next():
    facts = _facts(initiatives=[_init("a", started=False), _init("b", started=False)])
    assert plan_fill(facts, 1, frozenset({"a"})) == [{"kind": "launch_epic", "initiative": "b"}]


def test_epics_take_lanes_first_and_decomposes_get_the_remainder_oldest_first():
    facts = _facts(initiatives=[_init("a")], intake=["i1", "i2", "i3"])
    assert plan_fill(facts, 3) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
        {"kind": "launch_decompose", "intake_ids": ["i2"]},
    ]


def test_an_odd_remainder_of_one_lane_launches_no_decompose():
    facts = _facts(initiatives=[_init("a")], intake=["i1", "i2"])
    assert plan_fill(facts, 2) == [{"kind": "launch_epic", "initiative": "a"}]


def test_two_free_lanes_and_one_intake_item_launch_none():
    assert plan_fill(_facts(intake=["i1"]), 2) == []


def test_pull_is_planned_when_the_store_is_empty_and_sources_exist():
    assert plan_fill(_facts(sources_configured=True), 2) == [{"kind": "pull"}]


def test_no_pull_without_sources_or_when_the_store_has_work():
    assert plan_fill(_facts(sources_configured=False), 2) == []
    assert plan_fill(_facts(sources_configured=True, work_store_ready=True), 2) == []


def test_no_pull_when_ready_work_is_placed():
    facts = _facts(initiatives=[_init("a")], sources_configured=True)
    assert plan_fill(facts, 2) == [{"kind": "launch_epic", "initiative": "a"}]


def test_no_pull_when_intake_exists_even_if_it_launches_nothing():
    facts = _facts(intake=["i1"], sources_configured=True)
    assert plan_fill(facts, 2) == []
