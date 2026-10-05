from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_types import Facts, InitiativeFacts, RunningInitiative


def _init(
    id: str, surfaces: list[str] | None = None, repo: str | None = "r", requires: tuple[str, ...] = ()
) -> InitiativeFacts:
    extra = {**({"repo": repo} if repo else {}), **({"ready_surfaces": surfaces} if surfaces else {})}
    task = {"id": "t1", "needs": [], "requires": list(requires)}
    return {"id": id, "started": True, "ready_tasks": [task], "landed": set(), **extra}


def _run(id: str, surfaces: list[str], repo: str = "r") -> RunningInitiative:
    return {"id": id, "repo": repo, "surfaces": surfaces}


def _one_free_host(**over) -> Facts:
    return _facts(dispatch={"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 3}]}, **over)


def _facts(**over) -> Facts:
    base = {
        "lease": {"holder": "h", "host": "x", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.0, "hard_stop_fraction": 1.0, "launch_cap": 4, "go_degraded": False},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
    }
    return {**base, **over}


def _launch(id: str, **extra) -> dict:
    return {"kind": "launch_epic", "initiative": id, **extra}


def test_an_overlapping_initiative_defers_and_its_lane_goes_to_the_next_one():
    facts = _facts(
        initiatives=[_init("x", ["src/main.rs"]), _init("z", ["src/other.rs"])],
        running=[_run("y", ["src/main.rs"])],
    )
    assert plan_fill(facts, 1) == [
        _launch("z"),
        {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/main.rs"]},
    ]


def test_disjoint_initiatives_both_launch():
    facts = _facts(initiatives=[_init("x", ["src/a.rs"]), _init("z", ["src/b.rs"])], running=[_run("y", ["src/c.rs"])])
    assert plan_fill(facts, 2) == [_launch("x"), _launch("z")]


def test_a_directory_surface_defers_against_a_running_file_under_it():
    facts = _facts(initiatives=[_init("x", ["src/ui/"])], running=[_run("y", ["src/ui/regatta.rs"])])
    assert plan_fill(facts, 1) == [
        {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/ui/regatta.rs"]}
    ]


def test_surfaces_differing_only_under_snapshots_both_launch():
    facts = _facts(
        initiatives=[_init("x", ["src/ui/snapshots/a.snap"]), _init("z", ["src/ui/snapshots/b.snap"])],
        running=[_run("y", ["src/ui/snapshots/c.snap"])],
    )
    assert plan_fill(facts, 2) == [_launch("x"), _launch("z")]


def test_a_streak_of_three_asks_the_chair_and_launches_nothing_for_that_initiative():
    facts = _facts(
        initiatives=[_init("x", ["src/main.rs"])],
        running=[_run("y", ["src/main.rs"])],
        steer_streaks={"x|y": 3},
    )
    actions = plan_fill(facts, 1)
    assert [(a["kind"], a["initiative"]) for a in actions] == [("needs_chair", "x")]
    assert actions[0]["cause"] == "steer_deferred"


def test_two_overlapping_fresh_candidates_launch_the_first_and_defer_the_second_to_it():
    facts = _facts(initiatives=[_init("x", ["src/main.rs"]), _init("y", ["src/main.rs"])])
    assert plan_fill(facts, 2) == [
        _launch("x"),
        {"kind": "steer_clear", "initiative": "y", "other": "x", "paths": ["src/main.rs"]},
    ]


def test_claimed_initiatives_count_as_running():
    facts = _facts(initiatives=[_init("x", ["src/main.rs"])])
    claimed = [_run("y", ["src/main.rs"])]
    assert plan_fill(facts, 1, claimed=claimed) == [
        {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/main.rs"]}
    ]


def test_a_candidate_with_no_repo_or_no_surfaces_never_overlaps():
    facts = _facts(
        initiatives=[_init("x", ["src/main.rs"], repo=None), _init("w", None), _init("z", ["src/main.rs"])],
        running=[_run("y", ["src/main.rs"])],
    )
    assert plan_fill(facts, 3) == [
        _launch("x"),
        _launch("w"),
        {"kind": "steer_clear", "initiative": "z", "other": "y", "paths": ["src/main.rs"]},
    ]


def test_candidates_past_the_free_lanes_get_no_steer_check():
    facts = _facts(initiatives=[_init("x", ["src/a.rs"]), _init("z", ["src/main.rs"])], running=[_run("y", ["src/main.rs"])])
    assert plan_fill(facts, 1) == [_launch("x")]


def test_an_unplaceable_candidate_gives_the_host_lane_to_the_next_one():
    facts = _one_free_host(initiatives=[_init("a", requires=("go",)), _init("b")])
    assert plan_fill(facts, 0) == [_launch("b", host="jarvis")]


def test_after_the_local_lanes_an_unplaceable_candidate_is_skipped_for_the_host():
    facts = _one_free_host(initiatives=[_init("a"), _init("b", requires=("go",)), _init("c")])
    assert plan_fill(facts, 1) == [_launch("a"), _launch("c", host="jarvis")]


def test_an_unplaceable_candidate_claims_no_surfaces():
    facts = _one_free_host(initiatives=[_init("a", ["src/main.rs"], requires=("go",)), _init("b", ["src/main.rs"])])
    assert plan_fill(facts, 0) == [_launch("b", host="jarvis")]


def test_steer_actions_never_consume_a_lane_so_decomposes_and_the_pull_follow_them():
    facts = _facts(
        initiatives=[_init("x", ["src/main.rs"])],
        running=[_run("y", ["src/main.rs"])],
        intake=["i1", "i2"],
    )
    kinds = [a["kind"] for a in plan_fill(facts, 2)]
    assert kinds == ["steer_clear", "launch_decompose", "launch_decompose"]


def test_a_steered_out_initiative_is_dropped_before_host_placement():
    dispatch = {"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 2}], "local_lanes": "decompose"}
    facts = _facts(
        dispatch=dispatch,
        initiatives=[_init("x", ["src/main.rs"]), _init("z", ["src/other.rs"])],
        running=[_run("y", ["src/main.rs"])],
    )
    actions = plan_fill(facts, 2)
    assert actions == [
        _launch("z", host="jarvis"),
        {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/main.rs"]},
    ]
