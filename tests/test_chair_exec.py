import json
from dataclasses import replace

import pytest

from agent_tools import chair_exec, chair_housekeeping, remote_lane
from agent_tools.chair_exec import Deps, Refusal, argv_for, decompose_id, delete_branches_with, perform, run_argv, tail
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
    assert calls == [("record", "land"), ("record", "needs_chair")]


def test_clear_branches_refuses_a_foreign_glob() -> None:
    calls: list = []
    results = perform([_clear("*")], _deps(calls), lambda: 1, False)
    assert [r["status"] for r in results] == ["refused"]
    assert _touched(calls) == []


def test_clear_branches_deletes_its_own_glob_in_the_resolved_repo() -> None:
    calls: list = []
    results = perform([{**_clear("alpha"), "repo": "/w/app"}], _deps(calls), lambda: 1, False)
    assert _touched(calls) == [("delete", "/w/app", "epic/alpha/*")]
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
    assert _touched(calls) == [("run", ["cox", "route", "launch", "rescue", "--initiative", "work/a", "--task", "t1"])]
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
    assert argv_for({"kind": kind, "initiative": "x"})[-2:] == ["--initiative", "work/x"]  # type: ignore[typeddict-item,index]
    with_repo = argv_for({"kind": kind, "initiative": "x", "repo": "/r"})  # type: ignore[typeddict-item]
    assert with_repo[-4:] == ["--initiative", "work/x", "--repo", "/r"]  # type: ignore[index]


def test_a_launch_with_a_host_ends_with_on_that_host() -> None:
    assert argv_for({"kind": "launch_epic", "initiative": "x", "host": "jarvis"})[-2:] == ["--on", "jarvis"]  # type: ignore[index]
    assert argv_for({"kind": "launch_epic", "initiative": "x"}) == ["cox", "route", "launch", "epic", "--initiative", "work/x"]


def test_run_argv_runs_in_the_given_directory(tmp_path) -> None:
    code, output = run_argv(["pwd"], cwd=tmp_path)
    assert (code, output.strip()) == (0, str(tmp_path.resolve()))


def test_a_take_lease_over_an_expired_takeover_steals_and_a_plain_one_does_not() -> None:
    calls: list = []
    deps = replace(_deps(calls), acquire_lease=lambda holder, host, steal=False: calls.append(("lease", steal)) or "")
    perform([{"kind": "take_lease", "epoch": 1, "reason": "takeover expired at 2026-09-26T15:00:00+00:00"}], deps, lambda: 1, False)
    perform([{"kind": "take_lease", "epoch": 1}], deps, lambda: 1, False)
    assert [c for c in calls if c[0] == "lease"] == [("lease", True), ("lease", False)]


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
    assert argv_for(action, "alpha") == ["cox", "route", "launch", "decompose", "--idea", "intake/x.md", "--initiative-id", "alpha"]
    assert argv_for(action) is None


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
    assert _touched(calls) == [("run", ["cox", "route", "launch", "decompose", "--idea", "intake/my-idea.md", "--initiative-id", "my-idea"])]


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
