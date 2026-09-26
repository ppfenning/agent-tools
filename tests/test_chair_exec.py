from agent_tools.chair_exec import Deps, delete_branches_with, perform, run_argv

LANDED = "merge: ok\nmark_done: ok\n"


def _deps(calls: list, output: str = LANDED, code: int = 0, deleted: tuple[str, ...] = ("epic/alpha/t1",)) -> Deps:
    def run(argv):
        calls.append(("run", argv))
        return code, output

    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: calls.append(("delete", repo, pattern)) or (list(deleted), ""),
        acquire_lease=lambda holder, host: calls.append(("lease", holder)) or "",
        record=lambda action: calls.append(("record", action["kind"])),
        run_id=lambda action: "run-1",
        repo_for=lambda action: action.get("repo", "r"),
    )


def _land(task: str, repo: str) -> dict:
    return {"kind": "land", "task_id": task, "repo": repo, "run": "run-1", "epoch": 1}


def _clear(initiative: str) -> dict:
    return {"kind": "clear_branches", "initiative": initiative, "epoch": 1}


def test_a_fenced_action_is_refused_and_touches_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "pull", "epoch": 1}], _deps(calls), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced"]
    assert calls == []


def test_a_land_counts_only_when_both_markers_appear() -> None:
    results = perform([_land("t1", "r")], _deps([]), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]


def test_a_land_with_both_markers_but_a_nonzero_exit_is_not_counted() -> None:
    results = perform([_land("t1", "r")], _deps([], code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed", "escalated"]


def test_a_land_with_only_merge_is_not_counted_and_skips_its_repo_siblings() -> None:
    calls: list = []
    actions = [_land("t1", "r"), _land("t2", "r"), _clear("alpha"), _land("t3", "other")]
    results = perform(actions, _deps(calls, "merge: ok\n"), lambda: 1, False)
    assert [r["status"] for r in results if r["status"] != "escalated"] == ["not_landed", "skipped", "skipped", "not_landed"]
    assert "t1" in results[2]["reason"]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["t1", "t3"]


def test_a_land_missing_its_repo_is_refused_without_running() -> None:
    calls: list = []
    results = perform([{"kind": "land", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated"]
    assert calls == [("record", "needs_chair")]


def test_clear_branches_refuses_a_foreign_glob() -> None:
    calls: list = []
    results = perform([_clear("*")], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused"]
    assert calls == []


def test_clear_branches_deletes_its_own_glob_in_the_resolved_repo() -> None:
    calls: list = []
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], _deps(calls), lambda: 1, False)
    assert calls == [("delete", "/w/app", "epic/alpha/*")]
    assert results[0]["status"] == "done"
    assert "epic/alpha/t1" in results[0]["reason"]


def test_clear_branches_that_deletes_nothing_is_not_done() -> None:
    results = perform([_clear("alpha")], _deps([], deleted=()), lambda: 1, False)
    assert [r["status"] for r in results] == ["failed"]


def test_delete_branches_runs_git_in_the_named_repo_and_reports_what_it_deleted() -> None:
    argvs: list = []

    def run(argv):
        argvs.append(argv)
        return (0, "epic/alpha/t1\nepic/alphabet/x\n") if "for-each-ref" in argv else (0, "")

    assert delete_branches_with(run, "/w/app", "epic/alpha/*") == (["epic/alpha/t1"], "")
    assert all(a[:3] == ["git", "-C", "/w/app"] for a in argvs)
    assert argvs[1] == ["git", "-C", "/w/app", "branch", "-D", "epic/alpha/t1"]


def test_a_missing_binary_is_an_exit_code_not_an_exception() -> None:
    code, output = run_argv(["cox-binary-that-does-not-exist"])
    assert code == 127
    assert "cox-binary-that-does-not-exist" in output


def test_dry_run_performs_nothing() -> None:
    calls: list = []
    actions = [_land("t1", "r"), {"kind": "pull", "epoch": 1}]
    results = perform(actions, _deps(calls), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run", "dry_run"]
    assert calls == []


def test_a_rescue_launches_through_cox_route_launch_rescue() -> None:
    calls: list = []
    results = perform([{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 1, False)
    assert calls == [("run", ["cox", "route", "launch", "rescue", "--initiative", "work/a", "--task", "t1"])]
    assert [r["status"] for r in results] == ["done"]


def test_a_rescue_under_a_stale_epoch_is_fenced_and_calls_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced"]
    assert calls == []


def test_a_dry_run_rescue_performs_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run"]
    assert calls == []


def test_an_unlisted_kind_is_refused() -> None:
    calls: list = []
    results = perform([{"kind": "cut_release", "epoch": 1}], _deps(calls), lambda: 1, False)  # type: ignore[list-item]
    assert [r["status"] for r in results] == ["refused"]
    assert calls == []


def test_standby_and_needs_chair_are_recorded_without_running_anything() -> None:
    calls: list = []
    actions = [{"kind": "standby", "epoch": 1}, {"kind": "needs_chair", "epoch": 1, "initiative": "i"}]
    results = perform(actions, _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["recorded", "recorded"]
    assert calls == [("record", "standby"), ("record", "needs_chair")]
