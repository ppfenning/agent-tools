from agent_tools.chair_exec import Deps, argv_for, perform

STEER = {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/main.rs"], "epoch": 0}


def _run(action, epoch):
    ran, recorded = [], []
    deps = Deps(
        run=lambda argv: ran.append(argv) or (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda a: "run-1",
        repo_for=lambda a: "r",
    )
    return perform([action], deps, lambda: epoch, False), ran, recorded


def test_steer_clear_has_no_argv():
    assert argv_for({"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["src/main.rs"]}) is None


def test_steer_clear_is_recorded_without_running_a_command():
    results, ran, recorded = _run(STEER, 0)
    assert results[0]["status"] == "recorded"
    assert results[0]["reason"] == "steer clear of y: src/main.rs"
    assert ran == []
    assert (recorded[0]["initiative"], recorded[0]["other"], recorded[0]["paths"]) == ("x", "y", ["src/main.rs"])
    assert recorded[0]["status"] == "recorded"


def test_steer_clear_is_never_epoch_fenced():
    results, ran, _ = _run(STEER, -1)
    assert results[0]["status"] == "recorded"
    assert ran == []


def test_steer_clear_reason_is_cut_to_200_characters():
    action = {**STEER, "paths": ["p" * 150, "q" * 150]}
    results, _, _ = _run(action, 0)
    assert len(results[0]["reason"]) == 200
