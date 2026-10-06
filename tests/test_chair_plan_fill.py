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
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
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


def test_a_lone_free_local_lane_left_after_an_epic_launches_one_decompose():
    facts = _facts(initiatives=[_init("a")], intake=["i1", "i2"])
    assert plan_fill(facts, 2) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
    ]


def test_two_free_lanes_and_one_intake_item_launch_one_decompose():
    assert plan_fill(_facts(intake=["i1"]), 2) == [{"kind": "launch_decompose", "intake_ids": ["i1"]}]


def test_one_free_local_lane_with_a_lane_host_free_launches_one_decompose():
    facts = _hosted(2, intake=["i1", "i2"])
    assert plan_fill(facts, 1) == [{"kind": "launch_decompose", "intake_ids": ["i1"]}]


def test_three_free_local_lanes_launch_three_decomposes_oldest_first():
    facts = _hosted(2, intake=["i1", "i2", "i3", "i4"])
    assert plan_fill(facts, 3) == [{"kind": "launch_decompose", "intake_ids": [i]} for i in ("i1", "i2", "i3")]


def test_hosted_decompose_places_two_intakes_one_locally_and_one_on_the_host():
    facts = _hosted(2, intake=["i1", "i2"])
    assert plan_fill(facts, 1, hosted_decompose=True) == [
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
        {"kind": "launch_decompose", "intake_ids": ["i2"], "host": "jarvis"},
    ]


def test_hosted_decompose_takes_the_host_lane_before_an_epic_in_the_same_pass():
    facts = _hosted(1, initiatives=[_init("a"), _init("b")], intake=["i1", "i2"])
    assert plan_fill(facts, 1, hosted_decompose=True) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_decompose", "intake_ids": ["i1"], "host": "jarvis"},
    ]


def test_hosted_decompose_with_no_free_host_and_no_free_local_lane_places_nothing():
    assert plan_fill(_hosted(0, intake=["i1", "i2"]), 0, hosted_decompose=True) == []


def test_hosted_decompose_places_on_a_host_whatever_its_capabilities():
    dispatch = {"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "gpu", "live_runs": 3, "capabilities": ["cuda"]}]}
    assert plan_fill(_facts(dispatch=dispatch, intake=["i1"]), 0, hosted_decompose=True) == [
        {"kind": "launch_decompose", "intake_ids": ["i1"], "host": "gpu"},
    ]


def test_pull_is_planned_when_the_store_is_empty_and_sources_exist():
    assert plan_fill(_facts(sources_configured=True), 2) == [{"kind": "pull"}]


def test_no_pull_without_sources_or_when_the_store_has_work():
    assert plan_fill(_facts(sources_configured=False), 2) == []
    assert plan_fill(_facts(sources_configured=True, work_store_ready=True), 2) == []


def test_no_pull_when_ready_work_is_placed():
    facts = _facts(initiatives=[_init("a")], sources_configured=True)
    assert plan_fill(facts, 2) == [{"kind": "launch_epic", "initiative": "a"}]


def test_no_pull_when_intake_exists():
    facts = _facts(intake=["i1"], sources_configured=True)
    assert plan_fill(facts, 2) == [{"kind": "launch_decompose", "intake_ids": ["i1"]}]


def _hosted(free: int, **over) -> Facts:
    return _facts(dispatch={"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 4 - free}]}, **over)


def test_epics_past_the_local_lanes_fill_a_host_after_the_local_ones():
    facts = _hosted(2, initiatives=[_init("a"), _init("b"), _init("c")])
    assert plan_fill(facts, 1) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_epic", "initiative": "b", "host": "jarvis"},
        {"kind": "launch_epic", "initiative": "c", "host": "jarvis"},
    ]


def test_a_hosts_own_capacity_replaces_max_in_flight_in_its_free_count():
    dispatch = {"max_in_flight": 1, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 1, "capacity": 3}]}
    facts = _facts(dispatch=dispatch, initiatives=[_init("a"), _init("b"), _init("c")])
    assert [a.get("host") for a in plan_fill(facts, 1)] == [None, "jarvis", "jarvis"]


def test_a_host_with_a_free_lane_launches_an_epic_when_no_local_lane_is_free():
    assert plan_fill(_hosted(1, initiatives=[_init("a")]), 0) == [
        {"kind": "launch_epic", "initiative": "a", "host": "jarvis"}
    ]


def test_intake_stays_local_when_only_a_host_has_free_lanes():
    assert plan_fill(_hosted(4, intake=["i1", "i2"]), 0) == []


def _weighted_dispatch() -> dict:
    return {
        "max_in_flight": 4,
        "live_runs": 0,
        "hosts": [
            {"name": "big", "live_runs": 0, "weight": 3, "capacity": 4},
            {"name": "go-host", "live_runs": 3, "weight": 1, "capacity": 4, "capabilities": ["go"]},
        ],
    }


def test_hosts_fill_in_proportion_to_weight_whatever_their_order():
    heavy = {"name": "heavy", "live_runs": 0, "weight": 2}
    light = {"name": "light", "live_runs": 0, "weight": 1}
    for order in ([heavy, light], [light, heavy]):
        dispatch = {"max_in_flight": 4, "live_runs": 0, "hosts": order}
        facts = _facts(dispatch=dispatch, initiatives=[_init("a"), _init("b"), _init("c"), _init("d")])
        hosts = [a["host"] for a in plan_fill(facts, 0)]
        assert (hosts.count("heavy"), hosts.count("light")) == (3, 1)


def test_a_required_capability_places_only_on_a_host_that_has_it():
    facts = _facts(
        dispatch=_weighted_dispatch(),
        initiatives=[
            {
                "id": "a",
                "started": True,
                "ready_tasks": [{"id": "t1", "needs": [], "requires": ["go"]}],
                "landed": set(),
            }
        ],
    )
    assert plan_fill(facts, 0) == [{"kind": "launch_epic", "initiative": "a", "host": "go-host"}]


def test_an_initiative_with_no_requires_is_unaffected_by_host_capabilities():
    facts = _facts(dispatch=_weighted_dispatch(), initiatives=[_init("a")])
    assert plan_fill(facts, 0) == [{"kind": "launch_epic", "initiative": "a", "host": "big"}]


def _login_host(name: str, login_ok: bool) -> dict:
    return {"name": name, "state": "active", "versions_json": {"login_ok": login_ok}}


def test_a_blocked_hosts_free_count_is_zero_even_with_no_live_runs():
    dispatch = {"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 0}]}
    facts = _facts(dispatch=dispatch, initiatives=[_init("a")], login_hosts=[_login_host("jarvis", False)])
    assert plan_fill(facts, 0) == []


def test_an_unblocked_host_with_free_capacity_still_receives_a_launch():
    facts = _hosted(1, initiatives=[_init("a")], login_hosts=[_login_host("other", False)])
    assert plan_fill(facts, 0) == [{"kind": "launch_epic", "initiative": "a", "host": "jarvis"}]


def test_consumed_host_lanes_keeps_fill_off_a_lane_a_relaunch_just_took():
    facts = _hosted(1, initiatives=[_init("a")])
    assert plan_fill(facts, 0, consumed_host_lanes={"jarvis": 1}) == []


def _reserved(free: int, **over) -> Facts:
    """One lane host with `free` lanes, and the local lanes reserved for decomposes."""
    dispatch = {"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 4 - free}], "local_lanes": "decompose"}
    return _facts(dispatch=dispatch, **over)


def test_reserved_local_lanes_place_every_epic_on_a_host():
    facts = _reserved(1, initiatives=[_init("a"), _init("b")])
    assert plan_fill(facts, 2) == [{"kind": "launch_epic", "initiative": "a", "host": "jarvis"}]


def test_reserved_single_local_lane_takes_one_decompose_even_with_a_host_free():
    facts = _reserved(2, initiatives=[_init("a")], intake=["i1", "i2"])
    assert plan_fill(facts, 1) == [
        {"kind": "launch_epic", "initiative": "a", "host": "jarvis"},
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
    ]


def test_reserved_hosted_decompose_takes_the_only_host_lane_and_the_epic_waits():
    facts = _reserved(1, initiatives=[_init("a")], intake=["i1", "i2"])
    assert plan_fill(facts, 1, hosted_decompose=True) == [
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
        {"kind": "launch_decompose", "intake_ids": ["i2"], "host": "jarvis"},
    ]


def test_reserved_local_lanes_take_one_decompose_each():
    facts = _reserved(0, intake=["i1", "i2", "i3"])
    assert plan_fill(facts, 3) == [{"kind": "launch_decompose", "intake_ids": [i]} for i in ("i1", "i2", "i3")]


def test_reserved_mode_with_a_host_free_still_sends_epics_to_the_host_and_decomposes_to_local_lanes():
    facts = _reserved(2, initiatives=[_init("a"), _init("b")], intake=["i1", "i2", "i3"])
    assert plan_fill(facts, 3) == [
        {"kind": "launch_epic", "initiative": "a", "host": "jarvis"},
        {"kind": "launch_epic", "initiative": "b", "host": "jarvis"},
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
        {"kind": "launch_decompose", "intake_ids": ["i2"]},
        {"kind": "launch_decompose", "intake_ids": ["i3"]},
    ]


def test_reserved_local_lane_stays_empty_with_no_intake():
    assert plan_fill(_reserved(0, initiatives=[_init("a")]), 2) == []


def test_local_lanes_any_plans_exactly_what_an_absent_key_plans():
    over = {"initiatives": [_init("a")], "intake": ["i1", "i2", "i3"]}
    absent = _hosted(1, **over)
    any_ = _facts(dispatch={**absent["dispatch"], "local_lanes": "any"}, **over)
    assert plan_fill(any_, 3) == plan_fill(absent, 3) == [
        {"kind": "launch_epic", "initiative": "a"},
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
        {"kind": "launch_decompose", "intake_ids": ["i2"]},
    ]
