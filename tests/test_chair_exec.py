import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_tools import chair_exec, chair_housekeeping, remote_lane
from agent_tools.chair_exec import (
    Deps,
    Refusal,
    Run,
    argv_for,
    decompose_id,
    delete_branches_with,
    land_refusal,
    perform,
    run_argv,
    tail,
)
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_report import format_status
from agent_tools.store_url import TracesRoot

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


def _touched(calls: list) -> list:
    """The calls that reached cox, git or the lease: everything but the record."""
    return [c for c in calls if c[0] != "record"]


def test_a_fetch_runs_cox_runs_fetch_on_its_run():
    assert argv_for({"kind": "fetch", "run": "r-1"}) == ["cox", "runs", "fetch", "r-1"]


def test_a_fetch_exit_runs_the_same_cox_runs_fetch_argv_as_fetch():
    assert argv_for({"kind": "fetch_exit", "run": "r-1", "initiative": "i"}) == ["cox", "runs", "fetch", "r-1"]


def test_a_fetch_exit_that_exits_zero_applies_approvals_and_carries_run_and_host(tmp_path, monkeypatch) -> None:
    remote_lane.remote_record_path(tmp_path, "r-1").write_text(
        json.dumps({"host": "jarvis", "launched_at": "2026-09-26T00:00:00+00:00"}), encoding="utf-8"
    )
    calls: list = []

    def fake_apply(runs_dir, work_dir, run, initiative):
        calls.append((runs_dir, work_dir, run, initiative))
        return ["t1", "t2"]

    monkeypatch.setattr(chair_exec.chair_apply_fetch, "apply_fetched_approvals", fake_apply)
    deps = replace(_deps([]), runs_dir=tmp_path, work_dir=tmp_path)
    results = perform([{"kind": "fetch_exit", "run": "r-1", "initiative": "i", "epoch": 1}], deps, lambda: 1, False)
    assert calls == [(tmp_path, tmp_path, "r-1", "i")]
    assert (results[0]["status"], results[0]["run"], results[0]["host"]) == ("done", "r-1", "jarvis")
    assert results[0]["reason"] == "fetched r-1; approved: t1, t2"


def test_a_fetch_exit_that_exits_nonzero_applies_nothing_and_fails(tmp_path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(chair_exec.chair_apply_fetch, "apply_fetched_approvals", lambda *a: calls.append(a) or [])
    deps = replace(_deps([], code=1, output="boom"), runs_dir=tmp_path, work_dir=tmp_path)
    results = perform([{"kind": "fetch_exit", "run": "r-1", "initiative": "i", "epoch": 1}], deps, lambda: 1, False)
    assert calls == []
    assert results[0]["status"] == "failed"
    assert "run" not in results[0] and "host" not in results[0]


def _land(task: str, repo: str) -> dict:
    return {"kind": "land", "task_id": task, "repo": repo, "run": "run-1", "epoch": 1}


def _clear(initiative: str) -> dict:
    return {"kind": "clear_branches", "initiative": initiative, "epoch": 1}


def test_a_fenced_action_is_refused_and_touches_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "pull", "epoch": 1}], _deps(calls), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced"]
    assert calls == [("record", "pull")]


def test_land_refusal_is_none_on_a_clean_exit() -> None:
    assert land_refusal(_land("t1", "r"), 0, LANDED) is None


def test_land_refusal_names_a_conflict_from_the_output() -> None:
    # git cherry-pick's own conflict text, verbatim from a real conflict through `cli._execute_land_step`:
    # its "CONFLICT (content): ..." line goes to stdout, which that step drops whenever stderr is non-empty,
    # so the substring this rule reads is stderr's "error: could not apply ...", not "CONFLICT".
    output = (
        "error: could not apply a1b2c3d... Add seams module\n"
        "hint: After resolving the conflicts, mark them with\n"
        'hint: "git add/rm <pathspec>", then run\n'
        'hint: "git cherry-pick --continue".\n'
    )
    refusal = land_refusal(_land("t1", "r"), 1, output)
    assert refusal["cause"] == "conflict"


def test_land_refusal_names_failing_checks_from_the_output() -> None:
    refusal = land_refusal(_land("t1", "r"), 1, "failing checks: lint")
    assert refusal["cause"] == "checks"


def test_land_refusal_names_a_missing_branch_from_the_output() -> None:
    refusal = land_refusal(_land("t1", "r"), 1, "no candidate branch found for t1: tried agents/run-1/t1")
    assert refusal["cause"] == "missing_branch"


def test_land_refusal_falls_back_to_land_for_any_other_nonzero_exit() -> None:
    refusal = land_refusal(_land("t1", "r"), 1, "boom, something else broke")
    assert refusal["cause"] == "land"


def test_land_refusal_carries_the_action_s_initiative_and_task_id() -> None:
    action = {"kind": "land", "task_id": "t9", "initiative": "alpha", "repo": "r", "run": "run-1", "epoch": 3}
    assert land_refusal(action, 1, "boom") == {
        "kind": "needs_chair", "initiative": "alpha", "task_id": "t9", "cause": "land", "epoch": 3,
    }


def test_a_land_counts_only_when_both_markers_appear() -> None:
    results = perform([_land("t1", "r")], _deps([]), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]


def test_a_land_with_a_nonzero_exit_is_refused_and_escalated_with_its_cause() -> None:
    calls: list = []
    results = perform([_land("t1", "r")], _deps(calls, code=1), lambda: 1, False)
    assert [(r["action"]["kind"], r["status"]) for r in results] == [("land", "refused"), ("needs_chair", "escalated")]
    assert results[1]["action"]["cause"] == "land"
    assert [c for c in calls if c[0] == "record"] == [("record", "land"), ("record", "needs_chair")]


def test_a_refusing_land_blocks_its_repo_siblings_this_tick() -> None:
    calls: list = []
    actions = [_land("t1", "r"), _land("t2", "r")]
    output = "error: could not apply a1b2c3d... Add seams module\n"
    results = perform(actions, _deps(calls, output, code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "skipped", "escalated"]
    assert [r["action"].get("cause") for r in results if r["status"] == "escalated"] == ["conflict", STRANDED_CAUSE]
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--task", "t1", "--repo", "r", "--apply", "--no-claim"])]


def test_a_land_refused_before_it_runs_blocks_nothing() -> None:
    calls: list = []
    actions = [{"kind": "land", "task_id": "t1", "repo": "r", "epoch": 1}, _land("t2", "r")]
    results = perform(actions, _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "landed"]
    assert [c[1][5] for c in calls if c[0] == "run"] == ["t2"]


def test_the_status_line_names_a_refused_land_s_cause() -> None:
    facts = {
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "five_hour_fraction": None},
        "dispatch": {"max_in_flight": 4, "live_runs": 0},
    }
    land = {**_land("t1", "r"), "initiative": "alpha"}
    results = perform([land], _deps([], "failing checks: lint", code=1), lambda: 1, False)
    line = format_status(facts, [], results, datetime(2026, 9, 27, tzinfo=UTC))
    assert line.endswith("needs chair: alpha:checks")


def test_a_land_refused_by_the_repo_lease_is_busy_and_never_escalated() -> None:
    calls: list = []
    output = "land: refusing, pid 1 on host is landing in /repo"
    results = perform([_land("t1", "r")], _deps(calls, output), lambda: 1, False)
    assert [r["status"] for r in results] == ["busy"]
    assert ("record", "needs_chair") not in calls


def test_a_land_refused_for_any_other_reason_still_escalates_as_stranded() -> None:
    recorded: list = []
    deps = replace(_deps([], "land: refusing, /repo is dirty"), record=recorded.append)
    results = perform([_land("t1", "r")], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed", "escalated"]
    assert [(a["kind"], a.get("cause")) for a in recorded] == [("land", None), ("needs_chair", "stranded")]


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
    assert calls == [("record", "land"), ("record", "needs_chair")]


def test_clear_branches_refuses_a_foreign_glob() -> None:
    calls: list = []
    results = perform([_clear("*")], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused"]
    assert _touched(calls) == []


def test_clear_branches_deletes_its_own_glob_in_the_resolved_repo() -> None:
    calls: list = []
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"
    assert "epic/alpha/t1" in results[0]["reason"]


def test_clear_branches_that_finds_nothing_to_delete_is_done() -> None:
    results = perform([_clear("alpha")], _deps([], deleted=()), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert results[0]["reason"].startswith("nothing to clear")


def test_delete_branches_runs_git_in_the_named_repo_and_reports_what_it_deleted() -> None:
    argvs: list = []

    def run(argv):
        argvs.append(argv)
        return (0, "epic/alpha/t1\nepic/alphabet/x\n") if "for-each-ref" in argv else (0, "")

    assert delete_branches_with(run, "/w/app", "epic/alpha/*") == (["epic/alpha/t1"], "")
    assert all(a[:3] == ["git", "-C", "/w/app"] for a in argvs)
    assert argvs[1] == ["git", "-C", "/w/app", "branch", "-D", "epic/alpha/t1"]


_MAIN_WORKTREE = "worktree /w/app\nHEAD aaa\nbranch refs/heads/main"
_ALPHA_WORKTREE = "worktree /w/app/.worktrees/alpha-build\nHEAD bbb\nbranch refs/heads/epic/alpha/build"


def _porcelain_run(calls: list, listing: str, fail_remove_path: str = "") -> Run:
    """A fake git runner: `worktree list` returns `listing`; `worktree remove` on `fail_remove_path` fails."""

    def run(argv):
        calls.append(("run", argv))
        if "list" in argv:
            return 0, listing
        if "remove" in argv and fail_remove_path and fail_remove_path in argv:
            return 1, "boom"
        return 0, ""

    return run


def test_clear_branches_prunes_the_matching_worktree_then_deletes_its_branch() -> None:
    """The sweep finds nothing left after the prune deleted the only match; the clear is still done."""
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n"
    deps = replace(_deps(calls, deleted=()), run=_porcelain_run(calls, listing), repo_for=lambda action: "/w/app")
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], deps, lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("run", ["git", "-C", "/w/app", "worktree", "remove", "--force", "/w/app/.worktrees/alpha-build"]),
        ("run", ["git", "-C", "/w/app", "branch", "-D", "epic/alpha/build"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"


def test_clear_branches_with_no_matching_worktree_still_sweeps_the_pattern() -> None:
    """No live worktree to prune, so chair_plan_prune builds no argv; the pattern sweep clear_branches already makes today still runs."""
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n"
    deps = replace(_deps(calls), run=_porcelain_run(calls, listing), repo_for=lambda action: "/w/app")
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], deps, lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"


def test_clear_branches_stops_after_a_failing_worktree_remove() -> None:
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n"
    deps = replace(
        _deps(calls),
        run=_porcelain_run(calls, listing, fail_remove_path="/w/app/.worktrees/alpha-build"),
        repo_for=lambda action: "/w/app",
    )
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], deps, lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("run", ["git", "-C", "/w/app", "worktree", "remove", "--force", "/w/app/.worktrees/alpha-build"]),
    ]
    assert results[0]["status"] == "failed"


def test_a_failed_clear_branches_holds_back_its_relaunch_this_tick() -> None:
    calls: list = []
    listing = f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n"
    deps = replace(
        _deps(calls),
        run=_porcelain_run(calls, listing, fail_remove_path="/w/app/.worktrees/alpha-build"),
        repo_for=lambda action: "/w/app",
    )
    actions = [{**_clear("alpha"), "repo": "/w/app"}, {"kind": "relaunch", "initiative": "alpha", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "skipped"]
    assert not any(c[1][0] == "cox" for c in _touched(calls) if c[0] == "run")


def _git_repo(argvs: list, listing: str, branches: set) -> Run:
    """A stateful fake git: `branch -D` removes from `branches`, and `for-each-ref` lists what is left."""

    def run(argv):
        argvs.append(argv)
        if "list" in argv:
            return 0, listing
        if "for-each-ref" in argv:
            prefix = argv[-1].removeprefix("refs/heads/").removesuffix("*")
            return 0, "\n".join(sorted(b for b in branches if b.startswith(prefix)))
        if "-D" in argv:
            branches.discard(argv[-1])
        return 0, ""

    return run


def test_a_cleared_single_worktree_is_done_through_the_real_sweep_and_its_relaunch_runs() -> None:
    argvs: list = []
    branches = {"main", "epic/alpha/build"}
    run = _git_repo(argvs, f"{_MAIN_WORKTREE}\n\n{_ALPHA_WORKTREE}\n", branches)
    deps = replace(
        _deps([]), run=run, delete_branches=lambda repo, pattern: delete_branches_with(run, repo, pattern),
        repo_for=lambda action: "/w/app",
    )
    actions = [_clear("alpha"), {"kind": "relaunch", "initiative": "alpha", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["done", "done"]
    assert branches == {"main"}
    assert [a[3] if a[0] == "git" else a[0] for a in argvs] == ["worktree", "worktree", "branch", "for-each-ref", "cox"]


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
    assert _touched(calls) == [("run", ["cox", "route", "launch", "rescue", "--initiative", "work/a", "--task", "t1", "--no-claim"])]
    assert [r["status"] for r in results] == ["done"]


def test_a_rescue_under_a_stale_epoch_is_fenced_and_calls_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced"]
    assert _touched(calls) == []


def test_a_dry_run_rescue_performs_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}], _deps(calls), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run"]
    assert calls == []


def test_a_mark_lost_action_under_a_stale_epoch_is_fenced_and_calls_nothing() -> None:
    calls: list = []
    action = {"kind": "mark_lost", "initiative": "i", "run": "r1", "epoch": 1}
    results = perform([action], _deps(calls), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced"]
    assert _touched(calls) == []


def test_a_dry_run_mark_lost_performs_nothing() -> None:
    calls: list = []
    action = {"kind": "mark_lost", "initiative": "i", "run": "r1", "epoch": 1}
    results = perform([action], _deps(calls), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run"]
    assert calls == []


def test_an_unlisted_kind_is_refused() -> None:
    calls: list = []
    results = perform([{"kind": "cut_release", "epoch": 1}], _deps(calls), lambda: 1, False)  # type: ignore[list-item]
    assert [r["status"] for r in results] == ["refused"]
    assert _touched(calls) == []


def test_standby_and_needs_chair_are_recorded_without_running_anything() -> None:
    calls: list = []
    actions = [{"kind": "standby", "epoch": 1}, {"kind": "needs_chair", "epoch": 1, "initiative": "i"}]
    results = perform(actions, _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["recorded", "recorded"]
    assert calls == [("record", "standby"), ("record", "needs_chair")]


def test_a_mark_lost_action_is_recorded_without_running_anything() -> None:
    calls: list = []
    action = {"kind": "mark_lost", "epoch": 1, "initiative": "i", "run": "r1"}
    results = perform([action], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["recorded"]
    assert calls == [("record", "mark_lost")]


def test_a_standby_planned_at_epoch_minus_one_is_recorded_not_fenced() -> None:
    recorded: list = []
    deps = Deps(
        run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=recorded.append, run_id=lambda action: "", repo_for=lambda action: "",
    )
    results = perform([{"kind": "standby", "epoch": -1, "holder": "h"}], deps, lambda: 3, False)
    assert [r["status"] for r in results] == ["recorded"]
    assert [(a["kind"], a["status"]) for a in recorded] == [("standby", "recorded")]


def test_take_lease_is_never_fenced() -> None:
    calls: list = []
    results = perform([{"kind": "take_lease", "epoch": -1, "holder": "h", "host": "x"}], _deps(calls), lambda: 3, False)
    assert [r["status"] for r in results] == ["done"]
    assert calls == [("lease", "h"), ("record", "take_lease")]


def test_every_result_is_recorded_with_its_status_and_a_reason_cut_to_600_chars() -> None:
    recorded: list = []
    deps = Deps(
        run=lambda argv: (1, "x" * 700), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=recorded.append, run_id=lambda action: "", repo_for=lambda action: "",
    )
    actions = [{"kind": "rescue", "initiative": "a", "task_id": "t1", "epoch": 1}, {"kind": "pull", "epoch": 9}]
    perform(actions, deps, lambda: 1, False)
    assert [(a["kind"], a["status"]) for a in recorded] == [("rescue", "failed"), ("pull", "fenced")]
    assert recorded[0]["reason"] == "x" * 600
    assert recorded[0]["initiative"] == "a"


def test_a_dry_run_records_nothing() -> None:
    calls: list = []
    perform([{"kind": "standby", "epoch": -1}], _deps(calls), lambda: 1, True)
    assert calls == []


@pytest.mark.parametrize("kind", ["relaunch", "retry", "launch_epic"])
def test_an_epic_launch_names_the_initiative_by_its_work_path(kind: str) -> None:
    assert argv_for({"kind": kind, "initiative": "x"})[-3:] == ["--initiative", "work/x", "--no-claim"]  # type: ignore[typeddict-item,index]
    with_repo = argv_for({"kind": kind, "initiative": "x", "repo": "/r"})  # type: ignore[typeddict-item]
    assert with_repo[-5:] == ["--initiative", "work/x", "--no-claim", "--repo", "/r"]  # type: ignore[index]


def test_a_launch_with_a_host_ends_with_on_that_host() -> None:
    assert argv_for({"kind": "launch_epic", "initiative": "x", "host": "jarvis"})[-2:] == ["--on", "jarvis"]  # type: ignore[index]
    assert argv_for({"kind": "launch_epic", "initiative": "x"}) == ["cox", "route", "launch", "epic", "--initiative", "work/x", "--no-claim"]


def test_run_argv_runs_in_the_given_directory(tmp_path) -> None:
    code, output = run_argv(["pwd"], cwd=tmp_path)
    assert (code, output.strip()) == (0, str(tmp_path.resolve()))


def test_a_take_lease_over_an_expired_takeover_steals_and_a_plain_one_does_not() -> None:
    calls: list = []
    deps = replace(_deps(calls), acquire_lease=lambda holder, host, steal=False: calls.append(("lease", steal)) or "")
    perform([{"kind": "take_lease", "epoch": 1, "reason": "takeover expired at 2026-09-26T15:00:00+00:00"}], deps, lambda: 1, False)
    perform([{"kind": "take_lease", "epoch": 1}], deps, lambda: 1, False)
    assert [c for c in calls if c[0] == "lease"] == [("lease", True), ("lease", False)]


def test_the_loop_takes_the_lease_under_its_own_host_though_the_action_names_none(monkeypatch, tmp_path) -> None:
    taken: list = []
    monkeypatch.setattr(chair_exec.chair, "acquire_lease", lambda runs_dir, session, pid, host, steal=False: taken.append((session, pid, host)) or "")
    deps = chair_exec.edge_deps(tmp_path, tmp_path, "chair-loop", 42, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None, host="omarchy")
    perform([{"kind": "take_lease", "epoch": 1, "reason": "takeover expired at 2026-09-27T23:00:00+00:00"}], deps, lambda: 1, False)
    assert taken == [("chair-loop", 42, "omarchy")]


def test_an_intake_with_an_id_yields_that_id() -> None:
    assert decompose_id("intake/x.md", "alpha") == "alpha"


def test_an_intake_without_an_id_yields_its_stem() -> None:
    assert decompose_id("intake/my-idea.md", "") == "my-idea"


@pytest.mark.parametrize("bad", ["a/b", "..", ".hidden", "a\\b"])
def test_a_path_shaped_id_is_refused(bad: str) -> None:
    assert isinstance(decompose_id("intake/x.md", bad), Refusal)


def test_a_long_traceback_keeps_its_last_lines_and_not_its_head() -> None:
    frames = "".join(f'  File "m{i}.py", line {i}\n' for i in range(50))
    kept = tail("Traceback (most recent call last):\n" + frames + "ValueError: boom", 100)
    assert kept.endswith('  File "m49.py", line 49\nValueError: boom')
    assert "Traceback" not in kept and len(kept) <= 100


def test_a_short_string_is_unchanged_by_tail() -> None:
    assert tail("short", 200) == "short"


def test_a_decompose_argv_keeps_the_path_as_idea_and_uses_the_derived_id() -> None:
    action = {"kind": "launch_decompose", "intake_ids": ["intake/x.md"]}
    assert argv_for(action, "alpha") == ["cox", "route", "launch", "decompose", "--idea", "intake/x.md", "--initiative-id", "alpha", "--no-claim"]
    assert argv_for(action) is None


def _ticket(tmp_path, initiative: str, phase: str, ticket_id: str, title: str, body: str, surfaces: list[str], state: str = "ready"):
    path = tmp_path / "work" / initiative / phase / f"{ticket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    surfaces_line = "[" + ", ".join(surfaces) + "]"
    path.write_text(
        f"---\nid: {ticket_id}\nphase: {phase}\nstate: {state}\nneeds: []\nsurfaces: {surfaces_line}\ntitle: {title}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_initiative_tickets_reads_every_ticket_file_with_its_body(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    tickets = chair_exec._initiative_tickets(tmp_path, "alpha")
    assert [(t["id"], t["state"], t["surfaces"], t["body"]) for t in tickets] == [
        ("t1", "ready", ["agent_tools/shared.py"], "First body.")
    ]


def test_write_ticket_round_trips_through_initiative_tickets(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    [item] = chair_exec._initiative_tickets(tmp_path, "alpha")
    chair_exec._write_ticket(tmp_path, {**item, "title": "Renamed", "state": "dropped", "merged_into": "t2"})
    [reread] = chair_exec._initiative_tickets(tmp_path, "alpha")
    assert (reread["title"], reread["state"], reread["merged_into"]) == ("Renamed", "dropped", "t2")


def test_merge_and_write_collapses_two_same_phase_tickets_sharing_a_surface(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    _ticket(tmp_path, "alpha", "build", "t2", "Second ticket", "Second body.", ["agent_tools/shared.py"])
    chair_exec._merge_and_write(tmp_path, "alpha")
    merged_text = (tmp_path / "work" / "alpha" / "build" / "t1.md").read_text(encoding="utf-8")
    dropped_text = (tmp_path / "work" / "alpha" / "build" / "t2.md").read_text(encoding="utf-8")
    assert "title: First ticket; Second ticket" in merged_text
    assert "state: ready" in merged_text
    assert "state: dropped" in dropped_text
    assert "merged_into: t1" in dropped_text


def test_launch_epic_merges_same_phase_tickets_before_dispatching_the_merged_ticket(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    _ticket(tmp_path, "alpha", "build", "t2", "Second ticket", "Second body.", ["agent_tools/shared.py"])
    calls: list = []
    deps = replace(_deps(calls), work_dir=tmp_path)
    perform([{"kind": "launch_epic", "initiative": "alpha", "epoch": 1}], deps, lambda: 1, False)
    merged_text = (tmp_path / "work" / "alpha" / "build" / "t1.md").read_text(encoding="utf-8")
    dropped_text = (tmp_path / "work" / "alpha" / "build" / "t2.md").read_text(encoding="utf-8")
    assert "title: First ticket; Second ticket" in merged_text
    assert "state: dropped" in dropped_text and "merged_into: t1" in dropped_text
    assert [c for c in calls if c[0] == "run"] == [("run", ["cox", "route", "launch", "epic", "--initiative", "work/alpha", "--no-claim"])]


def test_relaunch_merges_same_phase_tickets_before_dispatching_the_merged_ticket(tmp_path) -> None:
    _ticket(tmp_path, "alpha", "build", "t1", "First ticket", "First body.", ["agent_tools/shared.py"])
    _ticket(tmp_path, "alpha", "build", "t2", "Second ticket", "Second body.", ["agent_tools/shared.py"])
    calls: list = []
    deps = replace(_deps(calls), work_dir=tmp_path)
    perform([{"kind": "relaunch", "initiative": "alpha", "epoch": 1}], deps, lambda: 1, False)
    merged_text = (tmp_path / "work" / "alpha" / "build" / "t1.md").read_text(encoding="utf-8")
    dropped_text = (tmp_path / "work" / "alpha" / "build" / "t2.md").read_text(encoding="utf-8")
    assert "title: First ticket; Second ticket" in merged_text
    assert "state: dropped" in dropped_text and "merged_into: t1" in dropped_text
    assert [c for c in calls if c[0] == "run"] == [("run", ["cox", "route", "launch", "epic", "--initiative", "work/alpha", "--no-claim"])]


def test_a_decompose_with_a_path_shaped_id_records_the_refusal_and_launches_nothing() -> None:
    calls: list = []
    recorded: list = []
    deps = replace(_deps(calls), intake_id=lambda path: "../x", record=recorded.append)
    perform([{"kind": "launch_decompose", "intake_ids": ["intake/x.md"], "epoch": 1}], deps, lambda: 1, False)
    assert _touched(calls) == []
    assert (recorded[0]["status"], "path-shaped" in recorded[0]["reason"]) == ("refused", True)


def test_a_decompose_without_an_id_field_launches_under_the_stem() -> None:
    calls: list = []
    perform([{"kind": "launch_decompose", "intake_ids": ["intake/my-idea.md"], "epoch": 1}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [("run", ["cox", "route", "launch", "decompose", "--idea", "intake/my-idea.md", "--initiative-id", "my-idea", "--no-claim"])]


_HK_PROBE = ["python", "-m", "harness.store_backfill_traces", "prune", "--help"]
_HK_PRUNE = ["python", "-m", "harness.store_backfill_traces", "prune", "t/traces", "--older-than", "7"]
_HK_SYNC = ["cox", "lake", "sync"]


def _hk_deps(monkeypatch, calls: list, fail: tuple = ()) -> Deps:
    """Fake housekeeping runner: any argv in `fail` exits 1, everything else exits 0; traces root is fixed to "t/traces"."""
    def run(argv: list) -> tuple[int, str]:
        calls.append(argv)
        return (1, f"boom: {argv[0]}") if argv in fail else (0, "ok")

    monkeypatch.setattr(chair_exec.run_store, "_traces_root", lambda runs_dir: TracesRoot("t/traces", False))
    return Deps(
        run=run, delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=lambda action: None, run_id=lambda action: "", repo_for=lambda action: "",
    )


def test_a_housekeeping_action_with_a_successful_runner_names_all_three_steps(monkeypatch) -> None:
    calls: list = []
    deps = _hk_deps(monkeypatch, calls)
    results = perform([{"kind": "housekeeping", "epoch": 1}], deps, lambda: 1, False)
    assert results[0]["status"] == "done"
    reason = results[0]["reason"]
    assert "lake sync: ok" in reason
    assert "prune: ok" in reason
    assert f"clean skipped: {chair_housekeeping.CLEAN_MISSING}" in reason
    assert _HK_PRUNE in calls


def test_a_failed_lake_sync_still_reports_the_prune_and_clean_outcomes(monkeypatch) -> None:
    calls: list = []
    deps = _hk_deps(monkeypatch, calls, fail=(_HK_SYNC,))
    results = perform([{"kind": "housekeeping", "epoch": 1}], deps, lambda: 1, False)
    assert results[0]["status"] == "failed"
    reason = results[0]["reason"]
    assert "lake sync: FAILED" in reason
    assert "prune: ok" in reason
    assert f"clean skipped: {chair_housekeeping.CLEAN_MISSING}" in reason


def test_a_dry_run_housekeeping_action_runs_no_step(monkeypatch) -> None:
    calls: list = []
    deps = _hk_deps(monkeypatch, calls)
    results = perform([{"kind": "housekeeping", "epoch": 1}], deps, lambda: 1, True)
    assert results[0]["status"] == "dry_run"
    assert calls == []


def test_a_prune_probe_that_fails_records_the_skip_reason(monkeypatch) -> None:
    calls: list = []
    deps = _hk_deps(monkeypatch, calls, fail=(_HK_PROBE,))
    results = perform([{"kind": "housekeeping", "epoch": 1}], deps, lambda: 1, False)
    reason = results[0]["reason"]
    assert chair_housekeeping.PRUNE_SKIPPED in reason
    assert "lake sync: ok" in reason
    assert f"clean skipped: {chair_housekeeping.CLEAN_MISSING}" in reason
    assert _HK_PRUNE not in calls
