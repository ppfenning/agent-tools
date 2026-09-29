import argparse
import json
import os
import subprocess as sp
import sys

import pytest

from agent_tools import cli
from agent_tools.chair_plan_land import plan_lands


def _land_ns(**overrides):
    base = {"run_id": "epic-x-5", "repo": None, "task": None, "phase": None, "label": None, "force": False,
                "worktree_root": "~/worktrees", "apply": False, "no_merge": False, "runs_dir": None, "profile": None, "gate": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def _work_item(work_root, initiative, phase, task_id, state):
    phase_dir = work_root / initiative / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / f"{task_id}.md").write_text(
        f"---\nid: {task_id}\nphase: {phase}\nstate: {state}\nneeds: []\ntitle: Add seams\n---\n\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture
def phase_runs_dir(tmp_path):
    runs_dir = tmp_path / "runs"
    tasks_dir = runs_dir / "epic-x-5" / "tasks" / "seams"
    tasks_dir.mkdir(parents=True)
    (runs_dir / "epic-x-5:seams.json").write_text(json.dumps({
        "phase": "seams", "initiative": "x", "phase_verdict": {"reasoning": "ok"},
    }), encoding="utf-8")
    (tasks_dir / "seams-task.json").write_text(json.dumps({
        "status": "done", "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "change_facts": {"fix_loop_attempts": 0, "files_touched": ["a.py"]}, "title": "Add seams",
        "proposals": [{"kind": "draft_pr_create", "title": "Add seams"}], "initiative": "x",
    }), encoding="utf-8")
    (tasks_dir / "seams-dropped.json").write_text(json.dumps({"status": "dropped"}), encoding="utf-8")
    _work_item(tmp_path / "work", "x", "seams", "seams-task", "done")
    _work_item(tmp_path / "work", "x", "seams", "seams-dropped", "dropped")
    (runs_dir / "policy.gate.json").write_text(json.dumps({"level": "full"}), encoding="utf-8")
    return runs_dir


def test_phase_mode_dry_run_step_list_has_no_checkout_step(phase_runs_dir, tmp_path, capsys):
    repo = tmp_path / "repo"; repo.mkdir()
    ns = _land_ns(repo=str(repo), phase="seams", runs_dir=str(phase_runs_dir))
    rc = cli._runs_land(ns)
    steps = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [s["kind"] for s in steps] == [
        "pick_branch", "squash_phase", "checks", "push", "pr_create", "wait_checks", "merge", "clean_phase", "mark_done",
    ]
    checks = next(s for s in steps if s["kind"] == "checks")
    assert checks["worktree_of"] == "pr/x--seams"
    clean = next(s for s in steps if s["kind"] == "clean_phase")
    assert clean["phase_branch"] == "epic/x/seams"
    assert clean["pr_branch"] == "pr/x--seams"
    assert clean["tasks"] == ["seams-task"]
    mark_done = next(s for s in steps if s["kind"] == "mark_done")
    assert mark_done["path"] == str(phase_runs_dir / "epic-x-5" / "tasks" / "seams" / "seams-task.json")


def test_phase_mode_dry_run_never_touches_the_filesystem_for_its_checks_step(phase_runs_dir, tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.tempfile, "mkdtemp", lambda *a, **k: calls.append(1) or str(tmp_path / "unused"))
    repo = tmp_path / "repo"; repo.mkdir()
    ns = _land_ns(repo=str(repo), phase="seams", runs_dir=str(phase_runs_dir))
    rc = cli._runs_land(ns)
    assert rc == 0
    assert calls == []


def test_default_mode_selects_phase_when_neither_flag_is_given(phase_runs_dir, tmp_path, capsys):
    repo = tmp_path / "repo"; repo.mkdir()
    ns = _land_ns(repo=str(repo), runs_dir=str(phase_runs_dir))
    rc = cli._runs_land(ns)
    steps = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [s["kind"] for s in steps] == [
        "pick_branch", "squash_phase", "checks", "push", "pr_create", "wait_checks", "merge", "clean_phase", "mark_done",
    ]


def test_task_flag_forces_task_mode_even_when_the_phase_has_two_records(phase_runs_dir, tmp_path, capsys):
    repo = tmp_path / "repo"; repo.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    ns = _land_ns(repo=str(repo), task="seams-task", runs_dir=str(phase_runs_dir))
    rc = cli._runs_land(ns)
    steps = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert steps == [{"kind": "refuse", "reason": "no branch is exactly one commit ahead of main", "found": {}}]


def test_resolved_gate_level_defaults_to_ticket_when_the_file_is_absent(tmp_path):
    assert cli._resolved_gate_level(tmp_path) == "ticket"


def test_resolved_gate_level_reads_the_level_cartridge_policy_dropped(tmp_path):
    (tmp_path / "policy.gate.json").write_text(json.dumps({"level": "epic"}), encoding="utf-8")
    assert cli._resolved_gate_level(tmp_path) == "epic"


def test_resolved_gate_level_reads_an_unknown_level_as_ticket_not_full(tmp_path):
    (tmp_path / "policy.gate.json").write_text(json.dumps({"level": "yolo"}), encoding="utf-8")
    assert cli._resolved_gate_level(tmp_path) == "ticket"


def test_absent_policy_truncates_the_dry_run_plan_at_ticket(phase_runs_dir, tmp_path, capsys):
    (phase_runs_dir / "policy.gate.json").unlink()
    repo = tmp_path / "repo"; repo.mkdir()
    rc = cli._runs_land(_land_ns(repo=str(repo), runs_dir=str(phase_runs_dir)))
    steps = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [s["kind"] for s in steps] == ["pick_branch", "squash_phase", "checks", "push", "pr_create", "note"]


def test_gate_flag_overrides_the_resolved_level_and_prints_that_it_did(phase_runs_dir, tmp_path, capsys):
    repo = tmp_path / "repo"; repo.mkdir()
    rc = cli._runs_land(_land_ns(repo=str(repo), runs_dir=str(phase_runs_dir), gate="ticket"))
    first_line, _, rest = capsys.readouterr().out.partition("\n")
    assert first_line == "land: --gate ticket overrides the resolved level"
    assert rc == 0
    assert [s["kind"] for s in json.loads(rest)][-1] == "note"


def _launcher_ns(profile_path, **overrides):
    base = {"launcher_profile": str(profile_path), "no_plugin": True, "print_argv": False}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def launcher_profile(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {workspace}\n", encoding="utf-8")
    return profile_path, workspace


def test__spawn_starts_a_detached_process(monkeypatch):
    calls = []

    class _Proc:
        pid = 4242

    def _fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)
    proc = cli._spawn(["x", "y"])
    assert proc.pid == 4242
    [(argv, kwargs)] = calls
    assert argv == ["x", "y"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == sp.DEVNULL


def test_bare_launcher_spawns_exactly_one_detached_beater_naming_its_pid(launcher_profile, monkeypatch, capsys):
    profile_path, workspace = launcher_profile
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(cli, "_route_chair_take", lambda a: 0)
    monkeypatch.setattr(cli.os, "chdir", lambda *a: None)
    monkeypatch.setattr(cli.os, "execvp", lambda *a: None)
    spawns = []

    class _Proc:
        pid = 9999

    def _fake_spawn(argv):
        spawns.append(argv)
        return _Proc()

    monkeypatch.setattr(cli, "_spawn", _fake_spawn)
    rc = cli._launcher(_launcher_ns(profile_path), [])
    out = capsys.readouterr().out
    assert rc == 0
    [argv] = spawns
    assert argv[:4] == [sys.executable, "-m", "agent_tools.chair", "beat-loop"]
    assert "--label" in argv and argv[argv.index("--label") + 1].startswith("chair-")
    assert "--pid" in argv and argv[argv.index("--pid") + 1] == str(os.getpid())
    assert "--runs-dir" in argv and argv[argv.index("--runs-dir") + 1] == str(workspace / "runs")
    assert "chair: beating from pid 9999" in out


def test_a_spawn_failure_is_printed_and_claude_still_execs(launcher_profile, monkeypatch, capsys):
    profile_path, _workspace = launcher_profile
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(cli, "_route_chair_take", lambda a: 0)
    monkeypatch.setattr(cli.os, "chdir", lambda *a: None)

    def _raising_spawn(argv):
        raise OSError("no such file or directory")

    monkeypatch.setattr(cli, "_spawn", _raising_spawn)
    execs = []
    monkeypatch.setattr(cli.os, "execvp", lambda *a: execs.append(a))
    rc = cli._launcher(_launcher_ns(profile_path), [])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chair: beater failed to start: no such file or directory" in out
    assert execs and execs[0][0] == "claude"


def test_print_argv_takes_no_lock_and_spawns_nothing(launcher_profile, monkeypatch, capsys):
    profile_path, _workspace = launcher_profile
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/claude")
    take_calls = []
    spawn_calls = []
    monkeypatch.setattr(cli, "_route_chair_take", lambda a: take_calls.append(a) or 0)
    monkeypatch.setattr(cli, "_spawn", lambda argv: spawn_calls.append(argv) or None)
    rc = cli._launcher(_launcher_ns(profile_path, print_argv=True), [])
    capsys.readouterr()
    assert rc == 0
    assert take_calls == []
    assert spawn_calls == []






def _clock():
    t, slept = [0.0], []
    return slept, (lambda s: (slept.append(s), t.__setitem__(0, t[0] + s))), (lambda: t[0])


def test_await_checks_reaches_green_after_two_empty_polls(capsys):
    answers = iter([(1, "no checks reported on the 'release/x' branch")] * 2 + [(0, "all passed")])
    slept, sleep, now = _clock()
    assert cli._await_checks(lambda: next(answers), sleep=sleep, now=now) == (True, "green")
    assert slept == [15, 15]
    assert capsys.readouterr().out.count("no checks reported yet") == 1


def test_await_checks_times_out_when_nothing_appears_within_the_grace_period():
    slept, sleep, now = _clock()
    result = cli._await_checks(lambda: (1, "no checks reported on the 'release/x' branch"), sleep=sleep, now=now)
    assert result == (False, "no checks reported within 180s")
    assert set(slept) == {15}


MOVED = "moved: run `uv run --frozen python -m devtools <command> ...` from the coxswain checkout (from 0.15.0)"


@pytest.mark.parametrize("argv", [
    ["dev"],
    ["dev", "release", "0.15.0", "--dry-run", "--manifest", "x.toml"],
    ["dev", "release-check", "--json"],
    ["release", "0.15.0", "--dry-run"],
])
def test_cox_dev_and_cox_release_print_the_devtools_pointer_and_exit_two(argv, capsys):
    assert cli.main(argv) == 2
    assert capsys.readouterr().out == MOVED + "\n"


_STORE_RUN = "x-9"
_STORE_LISTING = (
    "drwxr-xr-x          4,096 2026/09/25 05:00:00 .\n"
    "-rw-r--r--             20 2026/09/25 05:00:00 seams/seams-task.json\n"
)
_APPROVED_RECORD = {"review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"}}


def _store_fetch_world(tmp_path, monkeypatch, *, record):
    """A store-mode fetch with every seam faked: the remote lists one task, the store holds `record` for it."""
    ws, host_ws = tmp_path / "chair", tmp_path / "host"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / f"{_STORE_RUN}.remote.json").write_text(
        json.dumps({"host": "box", "launched_at": "2026-09-25T04:00:00Z"}), encoding="utf-8",
    )
    host = cli.lane_hosts.LaneHost(name="box", ssh="me@box", workspace_dir=str(host_ws))
    monkeypatch.setattr(cli, "_remote_edge", lambda cwd: (lambda argv: 0, lambda path: path))
    monkeypatch.setattr(cli, "_remote_capture", lambda cwd: lambda argv: _STORE_LISTING)
    monkeypatch.setattr(cli, "_remote_fetch_facts", lambda runs_dir, run: (True, "2026-09-25T05:03:46Z"))
    monkeypatch.setattr(cli.run_store, "run_task_ids", lambda runs_dir, run_id: ["seams-task"] if run_id == _STORE_RUN else [])
    monkeypatch.setattr(cli.run_store, "task_record_phases", lambda runs_dir, run_id, task: ["seams"])
    monkeypatch.setattr(cli.run_store, "task_state_of", lambda runs_dir, initiative, task: "ready")
    monkeypatch.setattr(
        cli.run_store, "task_record",
        lambda runs_dir, run_id, phase, task: {**record, "repo": str(host_ws / "repo")},
    )
    return ws, host


def test_store_mode_fetch_moves_an_approved_task_records_row_to_approved(tmp_path, monkeypatch):
    ws, host = _store_fetch_world(tmp_path, monkeypatch, record=_APPROVED_RECORD)
    _work_item(ws / "work", "x", "seams", "seams-task", "ready")
    moved = []

    def fake_set_state(runs_dir, initiative, task, state, by, expected=None):
        moved.append((initiative, task, state, expected))
        return cli.store_cli.StateSet({"state": state})

    monkeypatch.setattr(cli.store_cli, "set_state", fake_set_state)
    listings = []
    monkeypatch.setattr(cli, "_remote_capture", lambda cwd: lambda argv: listings.append(argv) or _STORE_LISTING)
    outcome, _, _ = cli._fetch_one(ws / "runs", [host], _STORE_RUN, mode="store")
    assert outcome == "fetched"
    assert moved == [("x", "seams-task", "approved", "ready")]
    assert len(listings) == 1  # the store-completeness check; the approval read never lists the remote


def test_the_approved_task_fact_built_from_a_store_row_carries_the_fetched_run_id(tmp_path, monkeypatch):
    ws, host = _store_fetch_world(tmp_path, monkeypatch, record=_APPROVED_RECORD)
    _work_item(ws / "work", "x", "seams", "seams-task", "ready")
    monkeypatch.setattr(cli.store_cli, "set_state", lambda *a, **k: cli.store_cli.StateSet({"state": "approved"}))
    records = cli._store_run_rows(ws / "runs", _STORE_RUN)
    facts = cli._store_approved_task_facts(ws / "runs", ws / "work", _STORE_RUN, records, "tester")
    assert facts == [{"id": "seams-task", "initiative": "x", "phase": "seams", "run": _STORE_RUN}]


def test_a_store_held_approved_row_reaches_the_chair_planner_as_a_land_phase_with_its_run(tmp_path, monkeypatch):
    ws, _ = _store_fetch_world(tmp_path, monkeypatch, record=_APPROVED_RECORD)
    (ws / "work" / "x").mkdir(parents=True)
    (ws / "work" / "x" / "initiative.md").write_text("---\nid: x\nrepo: /r\n---\n", encoding="utf-8")
    _work_item(ws / "work", "x", "seams", "seams-task", "ready")
    (ws / "runs" / f"{_STORE_RUN}.fetched.json").write_text(json.dumps({"fetched_at": "t", "repos": ["/r"]}), encoding="utf-8")
    store_items = [{"initiative": "x", "task_id": "seams-task", "phase": "seams", "state": "approved"}]
    monkeypatch.setattr(cli.run_store, "work_items", lambda runs_dir, initiative=None: store_items)
    records, items = cli._chair_stranded_inputs(ws, "store")
    stranded = cli.chair_read_stranded.read_stranded(records, items, lambda repo: True, lambda repo, branch: False)
    fetch_facts = cli.chair_read_approved.read_fetch_facts(ws / "runs", stranded)
    approved = cli.chair_read_approved.with_runs(cli.chair_read_approved.read_approved(ws, "store"), stranded, fetch_facts)
    actions = plan_lands({"approved": approved, "initiatives": [{"id": "x", "started": True, "ready_tasks": [], "landed": set()}]})
    assert [(a["kind"], a.get("run")) for a in actions] == [("land_phase", _STORE_RUN)]
