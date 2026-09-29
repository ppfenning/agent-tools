import argparse
import json
import os
import subprocess as sp
import sys
from datetime import UTC, datetime, timedelta

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


@pytest.fixture
def two_run_phase_dir(tmp_path):
    runs_dir = tmp_path / "runs"
    early, later = runs_dir / "x-2" / "tasks" / "seams", runs_dir / "x-3" / "tasks" / "seams"
    early.mkdir(parents=True)
    later.mkdir(parents=True)
    early_tasks = [f"a{i}" for i in range(1, 5)]
    later_tasks = [f"b{i}" for i in range(1, 4)]
    for task_id, task_dir in [(t, early) for t in early_tasks] + [(t, later) for t in later_tasks]:
        (task_dir / f"{task_id}.json").write_text(json.dumps({
            "status": "done", "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
            "change_facts": {"fix_loop_attempts": 0, "files_touched": ["a.py"]}, "title": task_id,
        }), encoding="utf-8")
        _work_item(tmp_path / "work", "x", "seams", task_id, "done")
    (runs_dir / "x-3:seams.json").write_text(json.dumps({
        "phase": "seams", "initiative": "x", "phase_verdict": {"reasoning": "ok"},
    }), encoding="utf-8")
    (runs_dir / "policy.gate.json").write_text(json.dumps({"level": "full"}), encoding="utf-8")
    return runs_dir


def test_phase_mode_lands_seven_tasks_split_across_an_earlier_and_the_current_run(two_run_phase_dir, tmp_path, capsys):
    repo = tmp_path / "repo"; repo.mkdir()
    ns = _land_ns(run_id="x-3", repo=str(repo), phase="seams", runs_dir=str(two_run_phase_dir))
    rc = cli._runs_land(ns)
    steps = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert not any(s["kind"] == "refuse" for s in steps)
    assert sum(1 for s in steps if s["kind"] == "mark_done") == 7


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


_METER_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
_DUMMY_ASSESSMENT = cli.pacing.Assessment(
    spent_fraction=0.0, elapsed_fraction=0.0, projected_total=0.0, headroom_usd=None,
    verdict="go", tier_ceiling="deep", effort_ceiling="high", hold_until=None, reason="ok",
)


def _tagged_window(tag: float) -> cli.pacing.Window:
    """A `Window` a test can pick back out of `usage_meter.prefer`'s result by `spent_usd` alone."""
    return cli.pacing.Window(
        start=_METER_NOW, end=_METER_NOW, spent_usd=tag, ceiling_usd=None, burn_usd_per_hour=0.0, runs_in_flight=0,
    )


def _fresh_meter() -> "cli.usage_meter.Meter":
    return cli.usage_meter.Meter(
        five_hour=cli.usage_meter.MeterEntry(used_percentage=40.0, resets_at=_METER_NOW + timedelta(hours=2)),
        seven_day=cli.usage_meter.MeterEntry(used_percentage=60.0, resets_at=_METER_NOW + timedelta(days=3)),
        observed_at=_METER_NOW,
    )


def _capture_assess(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        cli.pacing, "assess",
        lambda window, policy, now, weekly=None: captured.update(window=window, weekly=weekly) or _DUMMY_ASSESSMENT,
    )
    return captured


def test_usage_assessment_prefers_a_fresh_meter_window_for_both_figures(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _fresh_meter())
    monkeypatch.setattr(cli.usage_meter, "fresh", lambda meter, now, max_age=None: True)

    def _as_window(entry, now, span):
        return _tagged_window(111.0 if span == timedelta(hours=5) else 333.0)

    monkeypatch.setattr(cli.usage_meter, "as_window", _as_window)
    monkeypatch.setattr(cli.usage_meter, "implied_ceiling", lambda kind, now: None)
    recorded = []
    monkeypatch.setattr(
        cli.usage_meter, "record_implied_ceiling",
        lambda kind, entry, estimate_window, now: recorded.append((kind, estimate_window.spent_usd)),
    )
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _tagged_window(222.0))
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _tagged_window(444.0))
    captured = _capture_assess(monkeypatch)

    result = cli._usage_assessment(tmp_path, 50.0, 200.0, None)

    assert result is _DUMMY_ASSESSMENT
    assert captured["window"].spent_usd == 111.0
    assert captured["weekly"].spent_usd == 333.0
    # the calibration file is still refreshed from the estimate even though the meter won
    assert sorted(recorded) == [("five_hour", 222.0), ("weekly", 444.0)]


def test_usage_assessment_falls_back_to_the_estimate_when_the_meter_is_not_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _fresh_meter())
    monkeypatch.setattr(cli.usage_meter, "fresh", lambda meter, now, max_age=None: False)
    monkeypatch.setattr(
        cli.usage_meter, "as_window",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no meter window when the meter is not fresh")),
    )
    monkeypatch.setattr(cli.usage_meter, "implied_ceiling", lambda kind, now: 77.0 if kind == "five_hour" else None)
    recorded = []
    monkeypatch.setattr(cli.usage_meter, "record_implied_ceiling", lambda *a, **k: recorded.append(a))
    seen_ceilings: dict = {}

    def _gather(runs_dir, now, ceiling_usd=None, usage=None):
        seen_ceilings["five_hour"] = ceiling_usd
        return _tagged_window(222.0)

    def _gather_weekly(runs_dir, now, weekly_ceiling_usd=None, usage=None, reset=None):
        seen_ceilings["weekly"] = weekly_ceiling_usd
        return _tagged_window(444.0)

    monkeypatch.setattr(cli.usage_window, "gather", _gather)
    monkeypatch.setattr(cli.usage_window, "gather_weekly", _gather_weekly)
    captured = _capture_assess(monkeypatch)

    cli._usage_assessment(tmp_path, 50.0, 200.0, None)

    # five_hour: the recorded implied ceiling wins over the profile's 50.0; weekly: no implied
    # ceiling recorded, so the profile's 200.0 stays the fallback exactly as before this change.
    assert seen_ceilings == {"five_hour": 77.0, "weekly": 200.0}
    assert captured["window"].spent_usd == 222.0
    assert captured["weekly"].spent_usd == 444.0
    assert recorded == []  # meter not fresh: calibration is never rewritten


def _chair_run_deps_for(tmp_path, profile):
    runs = tmp_path / "runs"
    return cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")


def test_facts_deps_window_and_weekly_report_meter_when_the_meter_is_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: _fresh_meter())
    monkeypatch.setattr(cli.usage_meter, "fresh", lambda meter, now, max_age=None: True)
    monkeypatch.setattr(
        cli.usage_meter, "as_window",
        lambda entry, now, span: _tagged_window(111.0 if span == timedelta(hours=5) else 333.0),
    )
    monkeypatch.setattr(cli.usage_meter, "implied_ceiling", lambda kind, now: None)
    monkeypatch.setattr(cli.usage_meter, "record_implied_ceiling", lambda *a, **k: None)
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _tagged_window(222.0))
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _tagged_window(444.0))

    deps = _chair_run_deps_for(tmp_path, {})

    assert deps.facts_deps.window().spent_usd == 111.0
    assert deps.facts_deps.weekly().spent_usd == 333.0
    assert deps.facts_deps.window_source() == "meter"
    assert deps.facts_deps.weekly_source() == "meter"


def _meter_tick_fakes(monkeypatch, verdicts):
    """Counts meter reads; `fresh` answers from `verdicts` in call order, so a meter that ages past
    the freshness band mid-tick shows up as a True then a False."""
    reads = []
    answers = iter(verdicts)
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: reads.append(1) or _fresh_meter())
    monkeypatch.setattr(cli.usage_meter, "fresh", lambda meter, now, max_age=None: next(answers))
    monkeypatch.setattr(
        cli.usage_meter, "as_window",
        lambda entry, now, span: _tagged_window(111.0 if span == timedelta(hours=5) else 333.0),
    )
    monkeypatch.setattr(cli.usage_meter, "implied_ceiling", lambda kind, now: None)
    monkeypatch.setattr(cli.usage_meter, "record_implied_ceiling", lambda *a, **k: None)
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _tagged_window(222.0))
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _tagged_window(444.0))
    return reads


def test_facts_deps_read_the_meter_once_per_tick_and_labels_match_the_figure_it_built(monkeypatch, tmp_path):
    reads = _meter_tick_fakes(monkeypatch, [True, False, False, False])
    deps = _chair_run_deps_for(tmp_path, {})

    assert deps.facts_deps.window().spent_usd == 111.0
    assert deps.facts_deps.weekly().spent_usd == 333.0
    # a second freshness judgement would say False here; the label keeps the verdict the figure used
    assert (deps.facts_deps.window_source(), deps.facts_deps.weekly_source()) == ("meter", "meter")
    assert len(reads) == 1


def test_beat_starts_a_new_meter_tick(monkeypatch, tmp_path):
    reads = _meter_tick_fakes(monkeypatch, [True, False])
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", True, print, tmp_path / "profile.yaml", "files")

    assert deps.facts_deps.window_source() == "meter"
    deps.beat()
    assert deps.facts_deps.window().spent_usd == 222.0
    assert deps.facts_deps.window_source() == "est"
    assert len(reads) == 2


def test_facts_deps_window_and_weekly_report_est_when_there_is_no_fresh_meter(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.usage_meter, "read", lambda *a, **k: None)
    monkeypatch.setattr(cli.usage_meter, "implied_ceiling", lambda kind, now: None)
    monkeypatch.setattr(cli.usage_meter, "record_implied_ceiling", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("calibration is never written when there is no fresh meter")
    ))
    monkeypatch.setattr(cli.usage_window, "gather", lambda *a, **k: _tagged_window(222.0))
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *a, **k: _tagged_window(444.0))

    deps = _chair_run_deps_for(tmp_path, {})

    assert deps.facts_deps.window().spent_usd == 222.0
    assert deps.facts_deps.weekly().spent_usd == 444.0
    assert deps.facts_deps.window_source() == "est"
    assert deps.facts_deps.weekly_source() == "est"


def test_dash_once_prints_the_gathered_feed_as_one_json_line(monkeypatch, tmp_path, capsys):
    feed = {"runs": [], "as_of": "2026-09-29T00:00:00+00:00"}
    monkeypatch.setattr(cli.dash_feed, "gather_feed", lambda runs_dir, work_dir, now: feed)
    assert cli.main(["dash", "--once", "--runs-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == json.dumps(feed) + "\n"


def test_dash_feed_and_once_together_is_rejected(tmp_path, capsys):
    assert cli.main(["dash", "--feed", "--once", "--runs-dir", str(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "mutually exclusive" in err


def test_dash_feed_loops_printing_one_line_per_pass_until_stopped(monkeypatch, tmp_path, capsys):
    feed = {"runs": [], "as_of": "x"}
    monkeypatch.setattr(cli.dash_feed, "gather_feed", lambda runs_dir, work_dir, now: feed)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise StopIteration

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(StopIteration):
        cli.main(["dash", "--feed", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert out == (json.dumps(feed) + "\n") * 2
    assert sleeps == [2.0, 2.0]


def test_dash_detail_dispatches_run_with_the_run_id_runs_dir_and_now(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.dash_detail_run, "build", lambda *a: calls.append(a) or {"kind": "run"})
    assert cli.main(["dash", "--detail", "run", "r1", "--runs-dir", str(tmp_path)]) == 0
    (run_id, runs_dir, now), = calls
    assert run_id == "r1" and runs_dir == tmp_path and isinstance(now, str)
    assert json.loads(capsys.readouterr().out) == {"kind": "run"}


def test_dash_detail_dispatches_initiative_with_the_initiative_id_work_dir_runs_dir_and_now(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.dash_detail_initiative, "build", lambda *a: calls.append(a) or {"kind": "initiative"})
    assert cli.main([
        "dash", "--detail", "initiative", "i1", "--runs-dir", str(tmp_path), "--work-dir", str(tmp_path),
    ]) == 0
    (initiative_id, work_dir, runs_dir, now), = calls
    assert initiative_id == "i1" and work_dir == tmp_path and runs_dir == tmp_path and isinstance(now, str)
    assert json.loads(capsys.readouterr().out) == {"kind": "initiative"}


def test_dash_detail_dispatches_machine_with_the_host_name_runs_dir_and_now(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.dash_detail_machine, "build", lambda *a: calls.append(a) or {"kind": "machine"})
    assert cli.main(["dash", "--detail", "machine", "host-1", "--runs-dir", str(tmp_path)]) == 0
    (host_name, runs_dir, now), = calls
    assert host_name == "host-1" and runs_dir == tmp_path and isinstance(now, str)
    assert json.loads(capsys.readouterr().out) == {"kind": "machine"}


def test_dash_detail_dispatches_spend_with_runs_dir_and_now_and_no_id(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.dash_detail_spend, "build", lambda *a: calls.append(a) or {"kind": "spend"})
    assert cli.main(["dash", "--detail", "spend", "--runs-dir", str(tmp_path)]) == 0
    (runs_dir, now), = calls
    assert runs_dir == tmp_path and isinstance(now, str)
    assert json.loads(capsys.readouterr().out) == {"kind": "spend"}


def test_dash_detail_dispatches_health_with_runs_dir_work_dir_and_now_and_no_id(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.dash_detail_health, "build", lambda *a: calls.append(a) or {"kind": "health"})
    assert cli.main([
        "dash", "--detail", "health", "--runs-dir", str(tmp_path), "--work-dir", str(tmp_path),
    ]) == 0
    (runs_dir, work_dir, now), = calls
    assert runs_dir == tmp_path and work_dir == tmp_path and isinstance(now, str)
    assert json.loads(capsys.readouterr().out) == {"kind": "health"}


def test_dash_detail_dispatches_release_with_now_and_no_id(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(cli.dash_detail_release, "build", lambda *a: calls.append(a) or {"kind": "release"})
    assert cli.main(["dash", "--detail", "release", "--runs-dir", str(tmp_path)]) == 0
    (now,), = calls
    assert isinstance(now, str)
    assert json.loads(capsys.readouterr().out) == {"kind": "release"}


def test_dash_detail_run_without_an_id_exits_2(tmp_path, capsys):
    assert cli.main(["dash", "--detail", "run", "--runs-dir", str(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "an id is required" in err


def test_dash_detail_unknown_kind_exits_2_and_prints_nothing(tmp_path, capsys):
    assert cli.main(["dash", "--detail", "bogus", "x", "--runs-dir", str(tmp_path)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "unknown kind" in err
