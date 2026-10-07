import json
import shlex
import signal
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_housekeeping, chair_pid_probe, cli, courier, remote_lane, run_store
from agent_tools.chair_exec import (
    Deps,
    Refusal,
    Run,
    argv_for,
    decompose_id,
    delete_branches_with,
    land_commit,
    land_refusal,
    perform,
    run_argv,
    run_lane_host,
    smoke_targets,
    tail,
)
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_report import format_status
from agent_tools.draft_apply import plan_approve
from agent_tools.remote_argv import ssh_argv, sync_argv
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


def test_a_land_phase_runs_cox_runs_land_with_phase_not_task():
    action = {"kind": "land_phase", "run": "r-1", "phase": "p1", "repo": "r"}
    assert argv_for(action) == ["cox", "runs", "land", "r-1", "--phase", "p1", "--repo", "r", "--apply", "--no-claim"]


def test_a_land_phase_missing_its_phase_returns_no_argv():
    assert argv_for({"kind": "land_phase", "run": "r-1", "repo": "r"}) is None


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


def _land_phase(phase: str, repo: str) -> dict:
    return {"kind": "land_phase", "phase": phase, "repo": repo, "run": "run-1", "epoch": 1}


def _clear(initiative: str) -> dict:
    return {"kind": "clear_branches", "initiative": initiative, "epoch": 1}


def test_a_fenced_action_is_refused_and_touches_nothing() -> None:
    calls: list = []
    results = perform([{"kind": "pull", "epoch": 1}], _deps(calls), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced"]
    assert calls == [("record", "pull")]


def _carry(epoch: int = 1) -> dict:
    pick = {"task": "t1", "run": "run-1", "host": "h", "branch": "agents/run-1/t1", "commit": "abc", "needs": [], "run_seq": 1}
    return {"kind": "carry_phase", "initiative": "alpha", "phase": "p1", "pr_branch": "carry/alpha-p1", "picks": [pick], "epoch": epoch}


def _rebase(epoch: int = 1) -> dict:
    return {"kind": "rebase_phase", "initiative": "alpha", "phase": "p1", "branch": "epic/alpha/p1", "tip": "a" * 40, "base": "main", "epoch": epoch}


def _wired(calls: list, monkeypatch) -> Deps:
    def carry(action, git, forge, store):
        calls.append(("carry", action["phase"]))
        return {"action": action, "status": "done", "reason": ""}

    def rebase(action, port):
        calls.append(("rebase", action["branch"]))
        return {"action": action, "status": "done", "reason": ""}

    monkeypatch.setattr(chair_exec, "perform_carry", carry)
    monkeypatch.setattr(chair_exec, "perform_rebase", rebase)
    return replace(
        _deps(calls),
        carry_ports=lambda action: calls.append(("ports",)) or (None, None, None),
        rebase_port=lambda action: calls.append(("port",)),
    )


def test_a_carry_phase_reaches_the_carry_executor_and_is_recorded_once(monkeypatch) -> None:
    calls: list = []
    results = perform([_carry()], _wired(calls, monkeypatch), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert calls == [("ports",), ("carry", "p1"), ("record", "carry_phase")]


def test_a_rebase_phase_reaches_the_rebase_executor_and_is_recorded(monkeypatch) -> None:
    calls: list = []
    results = perform([_rebase()], _wired(calls, monkeypatch), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert calls == [("port",), ("rebase", "epic/alpha/p1"), ("record", "rebase_phase")]


def test_a_fenced_carry_phase_and_rebase_phase_call_nothing(monkeypatch) -> None:
    calls: list = []
    results = perform([_carry(), _rebase()], _wired(calls, monkeypatch), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced", "fenced"]
    assert calls == [("record", "carry_phase"), ("record", "rebase_phase")]


def test_a_dry_run_of_a_carry_phase_and_a_rebase_phase_performs_nothing(monkeypatch) -> None:
    calls: list = []
    results = perform([_carry(), _rebase()], _wired(calls, monkeypatch), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run", "dry_run"]
    assert calls == []


def test_a_carry_conflict_is_surfaced_as_needs_chair_and_marks_nothing_landed() -> None:
    calls: list = []

    class Git:
        def fetch_branch(self, host, branch): calls.append("fetch")
        def create_branch_from_main(self, name): calls.append("branch")
        def already_on_main(self, commit): return False
        def cherry_pick(self, commit): return ["a.py"]

    class Store:
        def mark_landed(self, task, run): calls.append("mark_landed")
        def set_done(self, task): calls.append("set_done")

    deps = replace(_deps(calls), carry_ports=lambda action: (Git(), None, Store()))
    results = perform([_carry()], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "recorded"]
    assert results[0]["needs_chair"]["kind"] == "needs_chair"
    assert "mark_landed" not in calls and "set_done" not in calls
    assert [c for c in calls if c[0] == "record"] == [("record", "carry_phase"), ("record", "needs_chair")]


def _store_edge(tmp_path, monkeypatch, pr_url: str) -> tuple[list, chair_exec._StoreEdge]:
    argvs: list = []

    def fake_run(build, run=None):
        argvs.append(build("py"))
        return 0, "{}"

    monkeypatch.setattr(chair_exec.store_cli, "_run", fake_run)
    monkeypatch.setattr(chair_exec.store_cli, "_store_url", lambda runs_dir: None)
    forge = chair_exec._ForgeEdge(None)
    forge.pr_url = pr_url
    return argvs, chair_exec._StoreEdge(tmp_path, tmp_path, _carry(), forge, lambda: "2026-10-05T00:00:00+00:00")


def test_the_store_edge_stamps_the_pr_url_open_pr_returned(tmp_path, monkeypatch) -> None:
    argvs, store = _store_edge(tmp_path, monkeypatch, "https://github.com/o/r/pull/7")
    store.mark_landed("t1", "run-1")
    assert argvs == [[
        "py", "-m", "harness.store_cli", "mark-landed", "run-1", "p1", "t1",
        "--pr", "https://github.com/o/r/pull/7", "--at", "2026-10-05T00:00:00+00:00",
    ]]


def test_the_store_edge_writes_no_landed_record_when_no_pr_was_opened(tmp_path, monkeypatch) -> None:
    argvs, store = _store_edge(tmp_path, monkeypatch, "")
    store.mark_landed("t1", "run-1")
    assert argvs == []


def test_the_forge_edge_keeps_only_the_url_gh_printed(monkeypatch) -> None:
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: (0, ""))
    monkeypatch.setattr(chair_exec.forge_auto, "open_pr", lambda *a, **k: (True, "Creating pull request\nhttps://github.com/o/r/pull/7\n"))
    forge = chair_exec._ForgeEdge(chair_exec._GitEdge(Path("."), Path("."), "r"))
    assert forge.open_pr("carry/alpha-p1", "t", "b") == "https://github.com/o/r/pull/7"
    assert forge.pr_url == "https://github.com/o/r/pull/7"


def test_the_rebase_edge_fetches_origin_then_pushes_origin_main_as_the_phase_tip(monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    chair_exec._RebaseEdge("/r").recreate_branch("epic/alpha/p1", "main")
    assert argvs == [
        ["git", "-C", "/r", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "-C", "/r", "push", "--force", "origin", "origin/main:refs/heads/epic/alpha/p1"],
    ]


def test_the_git_edge_fetches_origin_first_and_cuts_the_carry_branch_from_origin_main(tmp_path, monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    monkeypatch.setattr(chair_exec.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    git = chair_exec._GitEdge(tmp_path, tmp_path, "/r")
    git.create_branch_from_main("carry/alpha-p1")
    assert argvs[0] == ["git", "-C", "/r", "fetch", "origin"]
    assert argvs[-1] == ["git", "-C", "/r", "worktree", "add", "-B", "carry/alpha-p1", str(tmp_path), "origin/main"]


def test_the_git_edge_checks_a_pick_against_origin_main(monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (0, ""))
    assert chair_exec._GitEdge(Path("."), Path("."), "/r").already_on_main("abc") is True
    assert argvs[-1] == ["git", "-C", "/r", "merge-base", "--is-ancestor", "abc", "origin/main"]


def test_a_failed_fetch_of_origin_fails_the_carry_before_any_worktree_is_added(monkeypatch) -> None:
    argvs: list = []
    monkeypatch.setattr(chair_exec, "run_argv", lambda argv, cwd=None, timeout=None: argvs.append(argv) or (1, "no route"))
    with pytest.raises(RuntimeError, match="git fetch origin: no route"):
        chair_exec._GitEdge(Path("."), Path("."), "/r").create_branch_from_main("carry/alpha-p1")
    assert argvs == [["git", "-C", "/r", "fetch", "origin"]]


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


def test_a_repo_lease_refusal_that_exits_non_zero_is_still_busy_for_a_land_and_a_land_phase() -> None:
    # `cox runs land` exits 2 when another land holds the repository: busy, never refused or escalated.
    output = "land: refusing, chair-loop@bp-macbook:77598 is landing in /repo"
    for action in (_land("t1", "r"), _land_phase("p", "r")):
        calls: list = []
        results = perform([action], _deps(calls, output, code=2), lambda: 1, False)
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


def test_a_land_phase_counts_only_when_both_markers_appear() -> None:
    results = perform([_land_phase("p1", "r")], _deps([]), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]


def test_a_land_phase_runs_cox_runs_land_with_phase_repo_apply_no_claim() -> None:
    calls: list = []
    perform([_land_phase("p1", "r")], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--phase", "p1", "--repo", "r", "--apply", "--no-claim"])]


def test_a_land_phase_with_only_merge_is_not_counted_and_skips_its_repo_siblings() -> None:
    calls: list = []
    actions = [_land_phase("p1", "r"), _land("t2", "r")]
    results = perform(actions, _deps(calls, "merge: ok\n"), lambda: 1, False)
    assert [r["status"] for r in results if r["status"] != "escalated"] == ["not_landed", "skipped"]
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--phase", "p1", "--repo", "r", "--apply", "--no-claim"])]


def test_an_uncounted_land_phase_skips_a_later_land_phase_in_its_repo() -> None:
    calls: list = []
    actions = [_land_phase("p1", "r"), _land_phase("p2", "r")]
    results = perform(actions, _deps(calls, "merge: ok\n"), lambda: 1, False)
    assert [r["status"] for r in results] == ["not_landed", "escalated", "skipped", "escalated"]
    assert results[2]["reason"] == "an earlier land in r (phase p1) was not counted"
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1"]


def test_a_refused_land_phase_for_one_initiative_still_runs_another_initiative_s_relaunch_and_land_phase(tmp_path: Path) -> None:
    calls: list = []
    refused = {**_land_phase("p1", "r"), "initiative": "A"}
    actions = [
        refused,
        {"kind": "relaunch", "initiative": "B", "repo": "r", "epoch": 1},
        {**_land_phase("p2", "r"), "initiative": "B"},
    ]
    results = perform(actions, replace(_deps(calls, "failing checks: lint", code=1), work_dir=tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "failed", "refused", "escalated"]
    assert [c[1][:3] for c in calls if c[0] == "run"] == [["cox", "runs", "land"], ["cox", "route", "launch"], ["cox", "runs", "land"]]
    assert [c[1][5] for c in calls if c[0] == "run" and c[1][2] == "land"] == ["p1", "p2"]


def test_a_refused_land_phase_skips_a_later_action_for_the_same_initiative_in_another_repo() -> None:
    calls: list = []
    refused = {**_land_phase("p1", "r"), "initiative": "A"}
    actions = [refused, {**_land("t2", "r2"), "initiative": "A"}]
    results = perform(actions, _deps(calls, "failing checks: lint", code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "skipped", "escalated"]
    assert results[2]["reason"] == "an earlier land in r2 (phase p1) was not counted"
    assert [c[1][5] for c in calls if c[0] == "run"] == ["p1"]


def test_a_refused_land_phase_still_records_a_later_needs_chair_for_its_initiative() -> None:
    refused = {**_land_phase("p1", "r"), "initiative": "A"}
    actions = [refused, {"kind": "needs_chair", "initiative": "A", "epoch": 1}]
    results = perform(actions, _deps([], "failing checks: lint", code=1), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "escalated", "recorded"]


def test_a_refused_land_phase_escalates_naming_its_phase() -> None:
    facts = {
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "five_hour_fraction": None},
        "dispatch": {"max_in_flight": 4, "live_runs": 0},
    }
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    results = perform([action], _deps([], "failing checks: lint", code=1), lambda: 1, False)
    assert results[1]["action"] == {"kind": "needs_chair", "initiative": "alpha", "task_id": "", "cause": "checks", "epoch": 1, "phase": "p1"}
    assert results[1]["reason"] == "land phase p1 refused"
    line = format_status(facts, [], results, datetime(2026, 9, 27, tzinfo=UTC))
    assert line.endswith("needs chair: alpha/p1:checks")


def test_an_unclassified_land_phase_escalates_as_stranded_naming_its_phase() -> None:
    action = {**_land_phase("p1", "r"), "initiative": "alpha"}
    results = perform([action], _deps([], "merge: ok\n"), lambda: 1, False)
    assert results[1]["action"] == {"kind": "needs_chair", "initiative": "alpha", "task_id": "", "cause": STRANDED_CAUSE, "epoch": 1, "phase": "p1"}
    assert results[1]["reason"] == "land phase p1 not_landed"


def test_a_land_action_still_works_unchanged() -> None:
    calls: list = []
    results = perform([_land("t1", "r")], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["landed"]
    assert _touched(calls) == [("run", ["cox", "runs", "land", "run-1", "--task", "t1", "--repo", "r", "--apply", "--no-claim"])]


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


SSH_BOUND = ["ssh", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
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


_P_WORKTREE = "worktree /w/app/.worktrees/i-p\nHEAD ccc\nbranch refs/heads/epic/i/p"


def _carry_run(argvs: list, listing: str, branches: set, conflict_branches: frozenset = frozenset(), fail: tuple = ()) -> Run:
    """A stateful fake git: worktree list/add/remove, for-each-ref/branch -D, and a carried phase's merge/diff/abort.

    `fail`, when given, names the argv tokens (e.g. `("worktree", "add")`) whose exact command exits 1.
    """
    tmp_branch: dict[str, str] = {}

    def run(argv):
        argvs.append(argv)
        if fail and all(tok in argv for tok in fail):
            return 1, f"boom: {' '.join(fail)}"
        if "worktree" in argv and "list" in argv:
            return 0, listing
        if "rev-parse" in argv:
            return (0, "") if argv[-1].removeprefix("refs/heads/") in branches else (1, "")
        if argv[0] == "mktemp":
            return 0, "/tmp/cox-carry-abc\n"
        if "worktree" in argv and "add" in argv:
            tmp_branch[argv[-2]] = argv[-1]
            return 0, ""
        if "worktree" in argv and "remove" in argv:
            return 0, ""
        if "for-each-ref" in argv:
            prefix = argv[-1].removeprefix("refs/heads/").removesuffix("*")
            return 0, "\n".join(sorted(b for b in branches if b.startswith(prefix)))
        if "branch" in argv and "-D" in argv:
            branches.discard(argv[-1])
            return 0, ""
        if "merge" in argv and "--no-edit" in argv:
            return (1, "CONFLICT (content): merge conflict") if tmp_branch.get(argv[2]) in conflict_branches else (0, "merge ok")
        if "diff" in argv and "--diff-filter=U" in argv:
            return 0, "src/app.py\n"
        if "merge" in argv and "--abort" in argv:
            return 0, ""
        return 0, ""

    return run


def test_a_carried_phase_keeps_its_worktree_removed_branch_kept_then_merges_in_a_throwaway_worktree() -> None:
    argvs: list = []
    branches = {"epic/i/p"}
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n", branches)
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    results = perform([action], deps, lambda: 1, False)
    assert results[0]["status"] == "done"
    assert branches == {"epic/i/p"}
    assert not any("-D" in a for a in argvs)
    assert argvs[0] == ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]
    assert argvs[1] == ["git", "-C", "/w/app", "worktree", "remove", "--force", "/w/app/.worktrees/i-p"]
    assert argvs[2] == ["git", "-C", "/w/app", "for-each-ref", "--format=%(refname:short)", "refs/heads/epic/i/*"]
    add = argvs[3]
    tmp = add[-2]
    assert add == ["git", "-C", "/w/app", "worktree", "add", tmp, "epic/i/p"]
    assert argvs[4] == ["git", "-C", tmp, "merge", "--no-edit", "main"]
    assert argvs[5] == ["git", "-C", "/w/app", "worktree", "remove", "--force", tmp]
    assert len(argvs) == 6
    assert not any(a[0] == "ssh" for a in argvs)
    assert not any("checkout" in a or "switch" in a for a in argvs)


def test_a_carried_sweep_leaves_the_phase_branch_but_deletes_its_task_and_sibling_branches() -> None:
    argvs: list = []
    branches = {"epic/i/p", "epic/i/p--t", "epic/i/q"}
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n", branches)
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    results = perform([action], deps, lambda: 1, False)
    assert results[0]["status"] == "done"
    assert branches == {"epic/i/p"}


def test_a_conflicting_carry_merge_aborts_and_raises_one_needs_chair() -> None:
    argvs: list = []
    branches: set = set()
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n", branches, conflict_branches=frozenset({"epic/i/p"}))
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    action = {**_clear("i"), "carry": ["p"], "repo": "/w/app"}
    results = perform([action], deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["done", "recorded"]
    needs = results[1]
    assert needs["action"]["kind"] == "needs_chair" and needs["action"]["initiative"] == "i"
    assert "p" in needs["reason"] and "src/app.py" in needs["reason"]
    add = next(a for a in argvs if "worktree" in a and "add" in a)
    tmp = add[-2]
    assert ["git", "-C", tmp, "diff", "--name-only", "--diff-filter=U"] in argvs
    assert ["git", "-C", tmp, "merge", "--abort"] in argvs
    assert ["git", "-C", "/w/app", "worktree", "remove", "--force", tmp] in argvs
    assert not any("checkout" in a or "switch" in a for a in argvs)


def test_no_carry_key_runs_exactly_todays_argv_with_no_worktree_add_or_merge() -> None:
    calls: list = []
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [
        ("run", ["git", "-C", "/w/app", "worktree", "list", "--porcelain"]),
        ("delete", "/w/app", "epic/alpha/*"),
    ]
    assert results[0]["status"] == "done"
    assert not any(c[0] == "run" and ("add" in c[1] or "merge" in c[1]) for c in calls)


def test_a_failed_worktree_add_fails_the_clear_and_holds_its_relaunch() -> None:
    argvs: list = []
    branches: set = set()
    run = _carry_run(argvs, f"{_MAIN_WORKTREE}\n", branches, fail=("worktree", "add"))
    deps = replace(_deps([]), run=run, repo_for=lambda action: "/w/app")
    actions = [{**_clear("i"), "carry": ["p"], "repo": "/w/app"}, {"kind": "relaunch", "initiative": "i", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [r["status"] for r in results] == ["failed", "skipped"]
    assert "worktree add" in results[0]["reason"] and "epic/i/p" in results[0]["reason"]


def _host_carry_run(
    argvs: list,
    host_branches: set,
    conflict_branches: frozenset = frozenset(),
    host_listing: str = f"{_MAIN_WORKTREE}\n",
    chair_branches: frozenset = frozenset(),
    fail: tuple = (),
) -> Run:
    """A fake whose ssh argvs run against a host's own fake git and whose bare argvs run against the chair's."""
    chair = _carry_run([], f"{_MAIN_WORKTREE}\n", set(chair_branches))
    host = _carry_run([], host_listing, host_branches, conflict_branches, fail)

    def run(argv):
        argvs.append(argv)
        return host(shlex.split(argv[-1])) if argv[0] == "ssh" else chair(argv)

    return run


def _host_clear(run: Run, ssh: str = "me@jarvis", repo: str = "/w/app", relaunch: bool = False) -> list:
    deps = replace(_deps([]), run=run, repo_for=lambda action: repo, ssh_for=lambda host: ssh)
    clear = {**_clear("i"), "carry": ["p"], "repo": repo, "host": "jarvis"}
    tail_actions = [{"kind": "relaunch", "initiative": "i", "host": "jarvis", "epoch": 1}] if relaunch else []
    return perform([clear, *tail_actions], deps, lambda: 1, False)


def test_a_host_homed_carry_runs_its_git_steps_on_that_host() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p"}))
    assert results[0]["status"] == "done"
    assert ssh_argv("me@jarvis", sync_argv("/w/app")) in argvs
    assert [*SSH_BOUND, "me@jarvis", "git -C /tmp/cox-carry-abc merge --no-edit main"] in argvs
    assert [*SSH_BOUND, "me@jarvis", "git -C /w/app worktree remove --force /tmp/cox-carry-abc"] in argvs
    assert not any(a[0] == "git" and ("merge" in a or "add" in a) for a in argvs)


def test_a_phase_branch_present_only_on_the_host_is_carried_not_failed() -> None:
    results = _host_clear(_host_carry_run([], {"epic/i/p"}))
    assert results[0]["status"] == "done"
    assert results[0]["reason"] == "carried ['p'] past main on jarvis"


def test_a_host_worktree_holding_the_carried_branch_is_removed_on_the_host_before_the_add() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p", "epic/i/p--t"}, host_listing=f"{_MAIN_WORKTREE}\n\n{_P_WORKTREE}\n"))
    assert results[0]["status"] == "done"
    assert results[0]["reason"] == "deleted ['epic/i/p--t'] in /w/app on jarvis; carried ['p'] past main on jarvis"
    remove = [*SSH_BOUND, "me@jarvis", "git -C /w/app worktree remove --force /w/app/.worktrees/i-p"]
    add = [*SSH_BOUND, "me@jarvis", "git -C /w/app worktree add /tmp/cox-carry-abc epic/i/p"]
    assert argvs.index(remove) < argvs.index(add)
    assert [*SSH_BOUND, "me@jarvis", "git -C /w/app branch -D epic/i/p"] not in argvs


def test_a_phase_branch_only_the_chair_holds_is_pushed_to_the_host_then_carried_there() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, set(), chair_branches=frozenset({"epic/i/p"})))
    assert results[0]["reason"] == "carried ['p'] past main on jarvis"
    push = ["git", "-C", "/w/app", "push", "me@jarvis:/w/app", "refs/heads/epic/i/p:refs/heads/epic/i/p"]
    assert argvs.index(push) < argvs.index([*SSH_BOUND, "me@jarvis", "git -C /w/app worktree add /tmp/cox-carry-abc epic/i/p"])


def test_a_conflict_on_the_host_raises_carry_conflict() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p"}, frozenset({"epic/i/p"})))
    assert [r["status"] for r in results] == ["done", "recorded"]
    assert results[1]["action"]["cause"] == "carry_conflict"
    assert "src/app.py" in results[1]["reason"]
    assert [*SSH_BOUND, "me@jarvis", "git -C /tmp/cox-carry-abc merge --abort"] in argvs


def test_a_phase_branch_missing_on_both_machines_is_nothing_to_carry() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, set()))
    assert results[0]["status"] == "done"
    assert results[0]["reason"] == "nothing to clear: no branch matched epic/i/* in /w/app"
    assert not any(a[0] == "ssh" and "worktree add" in a[-1] for a in argvs)
    assert not any("push" in a for a in argvs)


def test_a_host_sync_failure_fails_the_clear_and_holds_the_relaunch_it_would_also_fail() -> None:
    results = _host_clear(_host_carry_run([], {"epic/i/p"}, fail=("bash",)), relaunch=True)
    assert [r["status"] for r in results] == ["failed", "skipped"]
    assert results[0]["reason"] == "updating /w/app on jarvis before the carry: boom: bash"


def test_a_host_with_no_ssh_destination_fails_the_clear_instead_of_carrying_locally() -> None:
    argvs: list = []
    results = _host_clear(_host_carry_run(argvs, {"epic/i/p"}), ssh="")
    assert results[0]["status"] == "failed"
    assert "jarvis" in results[0]["reason"]
    assert argvs == []


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _commit(checkout: str, name: str) -> None:
    (Path(checkout) / name).write_text(name, encoding="utf-8")
    _git("-C", checkout, "add", name)
    _git("-C", checkout, "commit", "-q", "-m", name)


def _configured(checkout: str) -> None:
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git("-C", checkout, "config", key, value)


def test_a_host_carry_runs_through_a_real_shell_against_real_git(tmp_path) -> None:
    """Each ssh argv runs as `bash -c` would on the host. The host's previous run still holds the phase branch in
    a worktree, and origin has a landed commit the host's main has not fetched yet."""
    origin, app, other, held = (str(tmp_path.resolve() / n) for n in ("origin.git", "app", "other", "held"))
    _git("init", "-q", "--bare", "-b", "main", origin)
    _git("init", "-q", "-b", "main", app)
    _configured(app)
    _commit(app, "a.txt")
    _git("-C", app, "remote", "add", "origin", origin)
    _git("-C", app, "push", "-q", "origin", "main")
    _git("-C", app, "worktree", "add", "-q", "-b", "epic/i/p", held)
    _commit(held, "b.txt")
    _git("clone", "-q", origin, other)
    _configured(other)
    _commit(other, "c.txt")
    _git("-C", other, "push", "-q", "origin", "main")
    chair = _carry_run([], f"{_MAIN_WORKTREE}\n", set())

    def run(argv):
        return run_argv(["bash", "-c", argv[-1]]) if argv[0] == "ssh" else chair(argv)

    results = _host_clear(run, repo=app)
    assert results[0]["status"] == "done", results[0]["reason"]
    assert results[0]["reason"] == "carried ['p'] past main on jarvis"
    assert f"worktree {held}\n" not in _git("-C", app, "worktree", "list", "--porcelain")
    assert _git("-C", app, "show", "epic/i/p:b.txt") == "b.txt"
    assert _git("-C", app, "show", "epic/i/p:c.txt") == "c.txt"


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


def test_a_check_login_action_calls_the_injected_edge_with_its_host_and_is_recorded() -> None:
    hosts_seen: list = []

    def fake_check_login(host: str) -> dict:
        hosts_seen.append(host)
        return {"name": host, "versions_json": {"login_ok": True}}

    deps = replace(_deps([]), check_login=fake_check_login)
    results = perform([{"kind": "check_login", "host": "shed", "epoch": 1}], deps, lambda: 1, False)
    assert hosts_seen == ["shed"]
    assert [r["status"] for r in results] == ["recorded"]


def test_a_fenced_check_login_action_calls_nothing() -> None:
    hosts_seen: list = []

    def fake_check_login(host: str) -> dict:
        hosts_seen.append(host)
        return {}

    deps = replace(_deps([]), check_login=fake_check_login)
    results = perform([{"kind": "check_login", "host": "shed", "epoch": 0}], deps, lambda: 1, False)
    assert hosts_seen == []
    assert [r["status"] for r in results] == ["fenced"]


def test_the_login_edge_passes_the_provider_profile_s_runner_and_env_names(monkeypatch, tmp_path) -> None:
    calls: list = []

    def fake_check_login_on_host(host, ssh, current_versions, now, ssh_run, cli_run, *, runner="claude-code", env_names=()):
        calls.append((runner, env_names))
        return {}

    monkeypatch.setattr(chair_exec.chair_login_check, "check_login_on_host", fake_check_login_on_host)
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "chair-loop", 1, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="omarchy", harness_python="python",
        provider_profile=lambda: {"runner": "openai-compatible", "auth_env": "MY_API_KEY"},
    )
    deps.check_login("shed")
    assert calls == [("openai-compatible", ("MY_API_KEY",))]


def _edge(tmp_path, **kwargs) -> Deps:
    return chair_exec.edge_deps(
        tmp_path, tmp_path, "chair-loop", 1, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="omarchy", harness_python="python", **kwargs,
    )


def test_a_dry_run_edge_reaches_no_lane_host(monkeypatch, tmp_path) -> None:
    reached: list = []
    monkeypatch.setattr(chair_exec.subprocess, "run", lambda argv, **kw: reached.append(argv))
    deps = _edge(tmp_path, dry_run=True)
    ssh = ["ssh", "shed", "true"]
    assert deps.run(ssh) == (1, chair_exec.NOT_PROBED)
    assert deps.run(["git", "push", "shed:/repo", "main"]) == (1, chair_exec.NOT_PROBED)
    assert deps.check_login("shed") == {}
    results = perform([{"kind": "fetch_exit", "run": "x-1", "epoch": 1}], deps, lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run"]
    assert reached == []


LANE_BEAT = "2026-10-05T11:59:00Z"


def _remote_lane_store(monkeypatch) -> None:
    fresh = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # the chair reads the real clock
    host = {"name": "h", "beat_at": fresh, "ssh": "me@h", "state": "active", "versions_json": '{"workspace_dir": "/ws"}'}
    lane = run_store.Lane("alpha-2", "h", "2026-10-05T10:00:00Z", LANE_BEAT)
    monkeypatch.setattr(chair_pid_probe.run_store, "hosts", lambda runs_dir: [host])
    monkeypatch.setattr(chair_pid_probe.run_store, "live_lanes", lambda runs_dir, now: [lane])
    monkeypatch.setattr(chair_pid_probe, "local_runs", lambda runs_dir, now: (0, set()))


def _chair_deps(monkeypatch, tmp_path, dry_run: bool, fetch):
    """The real `cox chair run` bundle over a store with one remote lane, its ssh door replaced by `fetch`."""
    _remote_lane_store(monkeypatch)
    monkeypatch.setattr(cli, "_pid_probe_ssh", fetch)
    return cli._chair_run_deps(tmp_path, {}, "chair", 1, "box", dry_run, print, tmp_path / "p.yaml", "files")


def test_a_dry_run_chair_bundle_never_calls_fetch_for_a_remote_lane_and_reads_empty(monkeypatch, tmp_path) -> None:
    def fetch_must_not_run(argv):
        raise AssertionError(f"fetch called in a dry run: {argv}")

    deps = _chair_deps(monkeypatch, tmp_path, True, fetch_must_not_run)
    assert deps.facts_deps.pid_probe() == {}


def test_a_live_chair_bundle_still_calls_fetch_once_for_a_remote_lane(monkeypatch, tmp_path) -> None:
    dialled: list = []

    def fetch(argv):
        dialled.append(argv)
        return 0, "alive\n"

    deps = _chair_deps(monkeypatch, tmp_path, False, fetch)
    assert deps.facts_deps.pid_probe() == {"alpha": {"alive": True, "last_beat_at": LANE_BEAT}}
    assert len(dialled) == 1 and dialled[0][-2] == "me@h"


def test_a_dry_run_chair_bundle_s_exec_door_refuses_a_lane_host(monkeypatch, tmp_path) -> None:
    deps = _chair_deps(monkeypatch, tmp_path, True, lambda argv: None)
    assert deps.exec_deps.run(["ssh", "shed", "true"]) == (1, chair_exec.NOT_PROBED)


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
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "chair-loop", 42, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="omarchy", harness_python="/venv/bin/python",
    )
    perform([{"kind": "take_lease", "epoch": 1, "reason": "takeover expired at 2026-09-27T23:00:00+00:00"}], deps, lambda: 1, False)
    assert taken == [("chair-loop", 42, "omarchy")]


def test_a_command_past_its_timeout_is_exit_124_not_an_exception() -> None:
    assert run_argv(["sleep", "5"], timeout=0.2)[0] == 124


def test_a_lane_host_ssh_that_times_out_fails_with_its_partial_output_and_is_not_retried(monkeypatch) -> None:
    seen: list = []

    def hung(argv, **kwargs):
        seen.append(kwargs)
        raise subprocess.TimeoutExpired(argv, 30, output=b"visit https://login.example/x\n", stderr="waiting\n")

    monkeypatch.setattr(chair_exec.subprocess, "run", hung)
    code, output = run_lane_host(["ssh", "h", "true"])
    assert code == 124
    assert "https://login.example/x" in output and "waiting" in output
    assert [kwargs["timeout"] for kwargs in seen] == [30]


def test_a_hung_lane_host_child_is_cut_off_at_the_bound_not_awaited(monkeypatch) -> None:
    monkeypatch.setattr(chair_exec, "LANE_HOST_TIMEOUT_S", 0.5)
    hung = ["sh", "-c", "echo partial; exec sleep 30"]
    started = time.monotonic()
    code, output = run_lane_host(hung)
    assert code == 124
    assert "partial" in output
    assert time.monotonic() - started < 3


def test_the_real_run_door_bounds_an_ssh_argv_and_leaves_any_other_unbounded(monkeypatch, tmp_path) -> None:
    seen: list = []
    monkeypatch.setattr(chair_exec.subprocess, "run", lambda argv, **kw: seen.append((argv[0], kw["timeout"])) or subprocess.CompletedProcess(argv, 0, "", ""))
    deps = chair_exec.edge_deps(
        tmp_path, tmp_path, "s", 1, run_id=lambda a: "", repo_for=lambda a: "", record=lambda a: None,
        host="h", harness_python="/venv/bin/python",
    )
    deps.run(["ssh", "h", "true"])
    deps.run(["git", "-C", "/r", "push", "u@h:/r", "refs/heads/b:refs/heads/b"])
    deps.run(["git", "status"])
    assert seen == [("ssh", 30), ("git", 30), ("git", None)]


def test_a_timed_out_branch_check_on_the_host_is_a_failure_not_an_absent_branch() -> None:
    remote: list = []
    local: list = []

    def at_home(argv):
        remote.append(argv)
        return 124, "ssh: timed out after 30s\nhalf a line"

    deps = replace(_deps([]), run=lambda argv: local.append(argv) or (1, ""))
    present, error = chair_exec._branch_on_host(deps, at_home, "u@h", "/r", "b")
    assert (present, len(remote), local) == (False, 1, [])
    assert "timed out after 30s" in error and "half a line" in error


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


def test_a_decompose_argv_under_sequence_ids_carries_initiative_id_and_task_ids_ordinal() -> None:
    action = {"kind": "launch_decompose", "intake_ids": ["intake/x.md"]}
    assert argv_for(action, "I412", "sequence") == [
        "cox", "route", "launch", "decompose", "--idea", "intake/x.md",
        "--initiative-id", "I412", "--task-ids", "ordinal", "--no-claim",
    ]


def test_a_decompose_argv_under_slug_ids_carries_no_task_ids_flag() -> None:
    action = {"kind": "launch_decompose", "intake_ids": ["intake/x.md"]}
    assert argv_for(action, "I412", "slug") == [
        "cox", "route", "launch", "decompose", "--idea", "intake/x.md", "--initiative-id", "I412", "--no-claim",
    ]


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


def test_a_decompose_under_sequence_ids_mode_launches_with_task_ids_ordinal() -> None:
    calls: list = []
    deps = replace(_deps(calls), intake_id=lambda path: "I412", ids_mode="sequence")
    perform([{"kind": "launch_decompose", "intake_ids": ["intake/x.md"], "epoch": 1}], deps, lambda: 1, False)
    assert _touched(calls) == [(
        "run",
        ["cox", "route", "launch", "decompose", "--idea", "intake/x.md", "--initiative-id", "I412", "--task-ids", "ordinal", "--no-claim"],
    )]


def test_prune_available_probes_the_harness_python() -> None:
    seen: list = []

    def run(argv: list) -> tuple[int, str]:
        seen.append(argv)
        return (0, "")

    assert chair_exec._prune_available(run, "/venv/bin/python") is True
    assert seen[0][0] == "/venv/bin/python"


_HK_PYTHON = "/venv/bin/python"
_HK_PROBE = [_HK_PYTHON, "-m", "harness.store_backfill_traces", "prune", "--help"]
_HK_PRUNE = [_HK_PYTHON, "-m", "harness.store_backfill_traces", "prune", "t/traces", "--older-than", "7"]
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
        harness_python=_HK_PYTHON,
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


def _write_stale_initiative(tmp_path, *, draft: str = "false") -> None:
    phase = tmp_path / "work" / "demo" / "phase-1"
    phase.mkdir(parents=True)
    (tmp_path / "work" / "demo" / "initiative.md").write_text(
        f"---\nid: demo\ndraft: {draft}\n---\nProse.\n", encoding="utf-8"
    )
    (phase / "t1.md").write_text("---\nid: t1\nstate: done\n---\nTicket body.\n", encoding="utf-8")


def _stale_action(**overrides) -> dict:
    return {
        "kind": "stale_to_draft", "initiative": "demo", "stale_tasks": ["t1"],
        "reason": "idle 10 days", "since": "2026-09-01T00:00:00Z", "epoch": 1, **overrides,
    }


def _stale_deps(calls: list, tmp_path) -> Deps:
    return replace(_deps(calls), work_dir=tmp_path, runs_dir=tmp_path, now=lambda: "2026-09-27T00:00:00+00:00")


def test_a_stale_to_draft_writes_the_rewritten_initiative_and_ticket_and_records_it(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    calls: list = []
    results = perform([_stale_action()], _stale_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "done"
    initiative_text = (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8")
    ticket_text = (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8")
    assert "draft: true" in initiative_text
    assert "state: todo" in ticket_text
    assert ("record", "stale_to_draft") in calls


def test_the_same_action_sends_one_courier_note_referencing_the_initiative(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    perform([_stale_action()], _stale_deps([], tmp_path), lambda: 1, False)
    blob = (tmp_path / "courier.jsonl").read_text(encoding="utf-8")
    # The inbox bare `cox` prints at start: its own chair label, as holder.
    [entry] = courier.inbox(blob, "chair-2026-09-27", holder="chair-2026-09-27")
    assert (entry["ref"], entry["note"]) == ("coxswain://initiative/demo", "idle 10 days")


def test_a_tick_acks_the_open_land_notice_of_a_done_task(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    phase = tmp_path / "work" / "demo" / "phase-1"
    (phase / "t2.md").write_text("---\nid: t2\nstate: todo\n---\nBody.\n", encoding="utf-8")
    lines = [
        courier.send(courier.Reference("task", task), "chair-loop", "chair", "land it", f"m-{task}")
        for task in ("t1", "t2")
    ]
    (tmp_path / "courier.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    perform([], _stale_deps([], tmp_path), lambda: 1, False)
    blob = (tmp_path / "courier.jsonl").read_text(encoding="utf-8")
    assert [e["id"] for e in courier.inbox(blob)] == ["m-t2"]
    perform([], _stale_deps([], tmp_path), lambda: 1, False)
    assert (tmp_path / "courier.jsonl").read_text(encoding="utf-8") == blob


def test_a_stale_to_draft_for_an_already_draft_initiative_writes_nothing(tmp_path) -> None:
    _write_stale_initiative(tmp_path, draft="true")
    before_initiative = (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8")
    before_ticket = (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8")
    results = perform([_stale_action()], _stale_deps([], tmp_path), lambda: 1, False)
    assert results[0]["status"] == "refused"
    assert (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8") == before_initiative
    assert (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8") == before_ticket
    assert not (tmp_path / "courier.jsonl").exists()


def test_a_fenced_stale_to_draft_is_fenced_and_calls_nothing(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    calls: list = []
    results = perform([_stale_action()], _stale_deps(calls, tmp_path), lambda: 2, False)
    assert results[0]["status"] == "fenced"
    assert _touched(calls) == []
    assert not (tmp_path / "courier.jsonl").exists()


def test_a_dry_run_stale_to_draft_performs_nothing(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    calls: list = []
    results = perform([_stale_action()], _stale_deps(calls, tmp_path), lambda: 1, True)
    assert results[0]["status"] == "dry_run"
    assert calls == []
    assert not (tmp_path / "courier.jsonl").exists()


def _signal_deps(calls: list, tmp_path) -> Deps:
    return replace(_deps(calls), runs_dir=tmp_path, send_signal=lambda pid, sig: calls.append(("signal", pid, sig)))


def test_a_stalled_usr1_with_a_pidfile_signals_the_pid_and_is_recorded_done(tmp_path) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []
    action = {"kind": "stalled_usr1", "run": "r1", "initiative": "i", "epoch": 1}
    results = perform([action], _signal_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "done"
    assert ("signal", 4242, signal.SIGUSR1) in calls
    assert ("record", "stalled_usr1") in [c for c in calls if c[0] == "record"]


def test_a_stalled_usr1_with_no_pidfile_signals_nothing_and_fails(tmp_path) -> None:
    calls: list = []
    action = {"kind": "stalled_usr1", "run": "r1", "initiative": "i", "epoch": 1}
    results = perform([action], _signal_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "failed"
    assert results[0]["reason"] == "no pidfile"
    assert [c for c in calls if c[0] == "signal"] == []


def test_a_stalled_stop_with_a_pidfile_signals_sigterm_and_is_recorded_done(tmp_path) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []
    action = {"kind": "stalled_stop", "run": "r1", "initiative": "i", "epoch": 1}
    results = perform([action], _signal_deps(calls, tmp_path), lambda: 1, False)
    assert results[0]["status"] == "done"
    assert ("signal", 4242, signal.SIGTERM) in calls


def test_a_stalled_stop_whose_pid_is_gone_is_recorded_failed_and_the_tick_goes_on(tmp_path) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError(3, "No such process")

    deps = replace(_deps(calls), runs_dir=tmp_path, send_signal=gone)
    actions = [{"kind": "stalled_stop", "run": "r1", "initiative": "i", "epoch": 1}, {"kind": "standby", "epoch": 1}]
    results = perform(actions, deps, lambda: 1, False)
    assert [(r["action"]["kind"], r["status"]) for r in results] == [("stalled_stop", "failed"), ("standby", "recorded")]
    assert calls == [("record", "stalled_stop"), ("record", "standby")]


@pytest.mark.parametrize("kind", ["stalled_usr1", "stalled_stop"])
def test_a_fenced_stalled_action_is_fenced_and_signals_nothing(tmp_path, kind: str) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []
    action = {"kind": kind, "run": "r1", "initiative": "i", "epoch": 1}
    results = perform([action], _signal_deps(calls, tmp_path), lambda: 2, False)
    assert results[0]["status"] == "fenced"
    assert [c for c in calls if c[0] == "signal"] == []


@pytest.mark.parametrize("kind", ["stalled_usr1", "stalled_stop"])
def test_a_dry_run_stalled_action_performs_nothing(tmp_path, kind: str) -> None:
    (tmp_path / "r1.pid").write_text("4242", encoding="utf-8")
    calls: list = []
    action = {"kind": kind, "run": "r1", "initiative": "i", "epoch": 1}
    results = perform([action], _signal_deps(calls, tmp_path), lambda: 1, True)
    assert results[0]["status"] == "dry_run"
    assert calls == []


def test_a_needs_chair_action_with_cause_stalled_is_recorded_through_the_generic_path() -> None:
    calls: list = []
    action = {"kind": "needs_chair", "initiative": "i", "cause": "stalled", "epoch": 1}
    results = perform([action], _deps(calls), lambda: 1, False)
    assert results[0]["status"] == "recorded"
    assert ("record", "needs_chair") in calls


def test_land_commit_matches_git_log_on_a_fixture_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda argv: subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=True)  # noqa: E731
    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "a@b.c"])
    run(["git", "config", "user.name", "tester"])
    (repo / "f.txt").write_text("one", encoding="utf-8")
    run(["git", "add", "f.txt"])
    run(["git", "commit", "-q", "-m", "first"])
    run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"])
    expected = run(["git", "log", "-1", "--format=%H", "origin/main"]).stdout.strip()
    assert land_commit(str(repo)) == expected


def test_land_commit_is_empty_on_a_git_failure(tmp_path) -> None:
    assert land_commit(str(tmp_path / "no-such-repo")) == ""


def _landed_result(repo: str, pr_url: str, commit: str) -> dict:
    reason = f"merge: ok\npr_create: {pr_url}\nmark_done: ok\n"
    return {"action": {"kind": "land", "repo": repo}, "status": "landed", "reason": reason, "commit": commit}


def test_smoke_targets_keeps_only_the_named_repos() -> None:
    # An action's repo is the initiative frontmatter's checkout path, e.g. `repo: /home/x/repos/coxswain-tools`.
    tools = _landed_result("/x/coxswain-tools", "https://github.com/org/coxswain-tools/pull/42", "abc123")
    umbrella = _landed_result("/x/coxswain", "https://github.com/org/coxswain/pull/9", "def456")
    assert smoke_targets([tools, umbrella]) == [{"repo": "/x/coxswain-tools", "pr": 42, "commit": "abc123"}]


def test_smoke_targets_matches_graphs_by_checkout_path_with_a_trailing_slash() -> None:
    graphs = _landed_result("/x/coxswain-graphs/", "https://github.com/org/coxswain-graphs/pull/7", "fed321")
    assert [t["pr"] for t in smoke_targets([graphs])] == [7]


def test_smoke_targets_reads_the_pr_from_the_land_step_lines_it_prints() -> None:
    # The shape `cli._land_walk` prints: one `<kind>: <detail>` line per step, `pr_create`'s detail being gh's PR URL.
    reason = (
        "forge: github\npick_branch: agents/r/t1 (Add seams)\ncherry_pick: cherry-picked a1b2c3d4 onto pr/t1\n"
        "checks: ok\npush: pushed pr/t1\npr_create: https://github.com/org/coxswain-tools/pull/118\n"
        "wait_checks: green\nmerge: merged\nclean: removed agents/r/t1\nmark_done: t1 done\n"
    )
    landed_result = {"action": {"kind": "land", "repo": "/x/coxswain-tools"}, "status": "landed", "reason": reason, "commit": "c0ffee"}
    assert smoke_targets([landed_result])[0]["pr"] == 118


def test_smoke_targets_ignores_a_pull_path_outside_the_pr_create_line() -> None:
    reason = "checks: see https://github.com/org/other/pull/5\npr_create: https://github.com/org/coxswain-tools/pull/9\nmerge: ok\nmark_done: ok\n"
    landed_result = {"action": {"kind": "land", "repo": "/x/coxswain-tools"}, "status": "landed", "reason": reason, "commit": "c"}
    assert smoke_targets([landed_result])[0]["pr"] == 9


def test_smoke_targets_skips_results_that_did_not_land() -> None:
    not_landed = {"action": {"kind": "land", "repo": "/x/coxswain-tools"}, "status": "not_landed", "reason": "boom"}
    assert smoke_targets([not_landed]) == []


def test_draft_apply_plan_approve_moves_the_written_stale_task_from_todo_to_ready(tmp_path) -> None:
    _write_stale_initiative(tmp_path)
    perform([_stale_action()], _stale_deps([], tmp_path), lambda: 1, False)
    initiative_text = (tmp_path / "work" / "demo" / "initiative.md").read_text(encoding="utf-8")
    ticket_text = (tmp_path / "work" / "demo" / "phase-1" / "t1.md").read_text(encoding="utf-8")
    plan = plan_approve(initiative_text, {"t1": ticket_text}, None, "pat", datetime(2026, 9, 27, tzinfo=UTC))
    assert plan.moves == (("t1", "todo", "ready"),)


def _tune(to_lanes: int = 3, epoch: int = 1) -> dict:
    return {
        "kind": "tune_lanes", "host": "hostA", "from_lanes": 2, "to_lanes": to_lanes, "epoch": epoch,
        "reason": "ahead of pace", "evidence": {"min_lanes": 1, "max_lanes": 4},
    }


def _propose(epoch: int = 1) -> dict:
    return {"kind": "propose_tiers", "proposals": [], "body": "cox settings set profile:tier.build opus", "epoch": epoch}


def _lane_only_writer(calls: list):
    def write(key: str, value: int) -> int:
        if not key.endswith(".capacity"):
            raise AssertionError(f"not a lane key: {key}")
        calls.append(("write", key, value))
        return 0

    return write


def _tune_deps(calls: list, tmp_path) -> Deps:
    write = _lane_only_writer(calls)
    return replace(
        _deps(calls), work_dir=tmp_path, runs_dir=tmp_path, set_lanes=lambda host, n: write(f"{host}.capacity", n),
    )


def test_a_tune_lanes_calls_the_lane_writer_once_and_records_one_action(tmp_path) -> None:
    calls: list = []
    results = perform([_tune()], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    assert calls == [("write", "hostA.capacity", 3), ("record", "tune_lanes")]


def test_a_tune_lanes_outside_min_or_max_writes_nothing(tmp_path) -> None:
    calls: list = []
    results = perform([_tune(0), _tune(5)], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused", "refused"]
    assert _touched(calls) == []


def test_a_propose_tiers_sends_one_inbox_item_with_the_body_and_records_one_action(tmp_path) -> None:
    calls: list = []
    results = perform([_propose()], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["done"]
    [entry] = courier.entries((tmp_path / "courier.jsonl").read_text(encoding="utf-8"))
    assert (entry["to"], entry["note"]) == ("chair", "cox settings set profile:tier.build opus")
    assert calls == [("record", "propose_tiers")]


def test_a_propose_tiers_calls_the_settings_writer_zero_times(tmp_path) -> None:
    calls: list = []
    perform([_propose()], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert _touched(calls) == []


def test_a_writer_that_raises_on_a_non_lane_key_is_never_tripped_by_either_action(tmp_path) -> None:
    calls: list = []
    results = perform([_tune(), _propose()], _tune_deps(calls, tmp_path), lambda: 1, False)
    assert [r["status"] for r in results] == ["done", "done"]


def test_a_fenced_tune_lanes_and_propose_tiers_call_nothing(tmp_path) -> None:
    calls: list = []
    results = perform([_tune(), _propose()], _tune_deps(calls, tmp_path), lambda: 2, False)
    assert [r["status"] for r in results] == ["fenced", "fenced"]
    assert _touched(calls) == []
    assert not (tmp_path / "courier.jsonl").exists()


def test_a_dry_run_of_tune_lanes_and_propose_tiers_performs_nothing(tmp_path) -> None:
    calls: list = []
    results = perform([_tune(), _propose()], _tune_deps(calls, tmp_path), lambda: 1, True)
    assert [r["status"] for r in results] == ["dry_run", "dry_run"]
    assert calls == []
    assert not (tmp_path / "courier.jsonl").exists()
