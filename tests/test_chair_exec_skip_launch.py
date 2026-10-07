from agent_tools.chair_exec import Deps, perform

LANDED = "merge: ok\nmark_done: ok\n"
UNCOUNTED = "merge: ok\n"


def _land_phase(initiative: str) -> dict:
    return {"kind": "land_phase", "initiative": initiative, "phase": "p1", "repo": "r", "run": "run-1", "epoch": 1}


def _launch_epic(initiative: str) -> dict:
    return {"kind": "launch_epic", "initiative": initiative, "epoch": 1}


def _run(actions: list, output: str) -> tuple[list, list]:
    ran: list = []
    deps = Deps(
        run=lambda argv: ran.append(argv) or (0, output),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
    )
    return perform(actions, deps, lambda: 1, False), ran


def test_a_launch_epic_after_an_uncounted_land_phase_is_skipped() -> None:
    results, ran = _run([_land_phase("alpha"), _launch_epic("alpha")], UNCOUNTED)
    assert [r["status"] for r in results if r["status"] != "escalated"] == ["not_landed", "skipped"]
    assert "was not counted" in results[-1]["reason"]
    assert [argv[:3] for argv in ran] == [["cox", "runs", "land"]]


def test_a_launch_epic_after_a_counted_land_phase_runs() -> None:
    results, ran = _run([_land_phase("alpha"), _launch_epic("alpha")], LANDED)
    assert [r["status"] for r in results] == ["landed", "done"]
    assert ran[-1][:4] == ["cox", "route", "launch", "epic"]


def test_an_uncounted_land_phase_leaves_another_initiative_s_launch_epic_running() -> None:
    results, ran = _run([_land_phase("alpha"), _launch_epic("beta")], UNCOUNTED)
    assert [r["status"] for r in results if r["status"] != "escalated"] == ["not_landed", "done"]
    assert ran[-1][:4] == ["cox", "route", "launch", "epic"]
