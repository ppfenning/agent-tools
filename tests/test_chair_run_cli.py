from __future__ import annotations

import contextlib
import dataclasses
import datetime
import json
import os

import pytest
from conftest import strip_ansi

from agent_tools import chair, chair_facts, chair_run, cli, store_cli


@pytest.fixture(autouse=True)
def _usable_provider(monkeypatch) -> None:
    """The suite's profile env var names no file, which `_lake_provider` reports as a problem; these tests stub the profile step too."""
    monkeypatch.setattr(cli, "_lake_provider", lambda _a: ({}, None))


def test_chair_run_parses_its_three_flags_with_their_defaults() -> None:
    parser = cli.build_parser()
    bare = parser.parse_args(["chair", "run"])
    given = parser.parse_args(["chair", "run", "--once", "--interval", "5", "--dry-run"])
    assert (bare.once, bare.interval, bare.dry_run) == (False, 60.0, False)
    assert (given.once, given.interval, given.dry_run) == (True, 5.0, True)


def test_chair_run_help_names_the_steal_flag_and_the_hard_stop(capsys) -> None:
    with contextlib.suppress(SystemExit):
        cli.build_parser().parse_args(["chair", "run", "--help"])
    out = strip_ansi(capsys.readouterr().out)
    assert "cox route chair take --steal" in out
    assert "weekly_hard_stop_fraction" in out


def test_the_real_bundle_names_no_source_it_cannot_read(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", False, print, tmp_path / "p.yaml", "files")
    assert isinstance(deps, chair_run.RunDeps)
    assert cli._chair_unwired_sources(deps) == []


def test_wired_fact_readers_alone_do_not_lift_the_refusal(monkeypatch, tmp_path, capsys) -> None:
    real = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", False, print, tmp_path / "p.yaml", "files")
    readers = {f.name: (lambda: None) for f in dataclasses.fields(real.facts_deps) if callable(getattr(real.facts_deps, f.name))}
    unwired = dataclasses.replace(real.exec_deps, record=cli._ChairUnwired("record"), run_id=cli._ChairUnwired("run_id"))
    wired = dataclasses.replace(real, facts_deps=dataclasses.replace(real.facts_deps, **readers), exec_deps=unwired)
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda _a: ({}, tmp_path, None))
    monkeypatch.setattr(cli, "_chair_run_deps", lambda *_a: wired)
    monkeypatch.setattr(cli.chair_run, "run", lambda *_a: (_ for _ in ()).throw(AssertionError("loop ran")))
    args = cli.build_parser().parse_args(["chair", "run"])
    assert cli._chair_unwired_sources(wired) == ["record", "run_id"]
    assert args.fn(args) == 2
    assert "not wired: record, run_id" in capsys.readouterr().out


def test_chair_run_refuses_before_any_beat_while_a_source_is_unwired(monkeypatch, tmp_path, capsys) -> None:
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda _a: ({}, runs_dir, None))
    real_deps = cli._chair_run_deps

    def one_unwired(*a):
        deps = real_deps(*a)
        return dataclasses.replace(deps, exec_deps=dataclasses.replace(deps.exec_deps, record=cli._ChairUnwired("record")))

    monkeypatch.setattr(cli, "_chair_run_deps", one_unwired)
    monkeypatch.setattr(cli.chair, "renew_lease", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("beat ran")))
    monkeypatch.setattr(cli.chair_run, "run", lambda *_a: (_ for _ in ()).throw(AssertionError("loop ran")))
    args = cli.build_parser().parse_args(["chair", "run", "--once"])
    assert args.fn(args) == 2
    assert "refusing to start, no lease taken; not wired: record" in capsys.readouterr().out
    assert not runs_dir.exists()


def test_a_dry_run_beat_touches_no_lease(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli.chair, "renew_lease", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("beat ran")))
    cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", True, print, tmp_path / "p.yaml", "files").beat()
    assert list(tmp_path.iterdir()) == []


def test_once_exits_1_on_a_tick_error_and_releases_a_held_lease(monkeypatch, tmp_path) -> None:
    released: list[str] = []
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda _a: ({}, tmp_path, None))
    monkeypatch.setattr(cli, "_chair_unwired_sources", lambda _f: [])
    monkeypatch.setattr(cli.chair, "_read_lease", lambda *_a: {"epoch": 3})
    monkeypatch.setattr(cli.chair, "release_lease", lambda *_a: released.append("released"))
    monkeypatch.setattr(cli.chair_run, "run", lambda once, interval, dry_run, deps: deps.report_deps.echo("chair 09-26 | tick error: RuntimeError: x"))
    args = cli.build_parser().parse_args(["chair", "run", "--once"])
    assert args.fn(args) == 1
    assert released == ["released"]


def test_once_exit_is_tied_to_the_line_chair_run_prints_for_a_tick_error() -> None:
    now = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)
    assert (cli._chair_once_exit(chair_run.error_line(RuntimeError("x"), now)), cli._chair_once_exit("chair 09-26 | holding")) == (1, 0)


def test_the_stranded_source_reads_item_state_from_the_store_under_store_mode(monkeypatch, tmp_path) -> None:
    (tmp_path / "work" / "i1" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "i1" / "p1" / "t1.md").write_text("---\nid: t1\nstate: approved\n---\n", encoding="utf-8")
    (tmp_path / "runs" / "r1" / "tasks" / "p1").mkdir(parents=True)
    record = {"initiative": "i1", "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"}}
    (tmp_path / "runs" / "r1" / "tasks" / "p1" / "t1.json").write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(cli.run_store, "work_items", lambda *_a: [{"initiative": "i1", "task_id": "t1", "state": "done"}])

    def stranded(mode: str) -> list:
        return cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", True, print, tmp_path / "p.yaml", mode).facts_deps.stranded()

    assert ([row["task"] for row in stranded("files")], stranded("store")) == (["t1"], [])


def _tick_facts(runs_dir, policy_text: str | None):
    runs_dir.mkdir()
    if policy_text is not None:
        (runs_dir / "policy.pacing.json").write_text(policy_text, encoding="utf-8")
    deps = cli._chair_run_deps(runs_dir, {}, "chair", 1, "h", True, print, runs_dir / "p.yaml", "files")
    return chair_facts.gather_facts(deps.facts_deps, datetime.datetime.now(datetime.UTC))


def test_the_launch_cap_is_three_lanes_unless_the_policy_file_names_a_valid_other(tmp_path) -> None:
    configured = _tick_facts(tmp_path / "a", json.dumps({"max_in_flight": 2}))["dispatch"]["max_in_flight"]
    absent = _tick_facts(tmp_path / "b", None)["dispatch"]["max_in_flight"]
    invalid = _tick_facts(tmp_path / "c", json.dumps({"max_in_flight": 0}))["dispatch"]["max_in_flight"]
    garbled = _tick_facts(tmp_path / "d", "{")["dispatch"]["max_in_flight"]
    assert (configured, absent, invalid, garbled) == (2, 3, 3, 3)


def test_a_beat_ends_the_docket_snapshot_so_each_tick_reads_it_once(monkeypatch, tmp_path) -> None:
    reads: list[int] = []
    monkeypatch.setattr(cli.chair_read_docket, "read_docket", lambda *_a: reads.append(1) or {"initiatives": [], "n": len(reads)})
    monkeypatch.setattr(cli.chair_read_live, "read_live_initiatives", lambda *_a: [])
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", True, print, tmp_path / "p.yaml", "files")
    facts = deps.facts_deps
    first = (facts.docket()["n"], facts.live_initiatives(), facts.work_store_ready(), facts.docket()["n"])
    deps.beat()
    assert (first, facts.docket()["n"], len(reads)) == ((1, [], False, 1), 2, 2)


def test_each_fact_reader_reaches_its_own_module_with_the_workspace_and_mode(monkeypatch, tmp_path) -> None:
    runs_dir, profile_path = tmp_path / "runs", tmp_path / "p.yaml"
    reader = cli.chair_read_approved, cli.chair_read_quarantined, cli.chair_read_attempts, cli.chair_read_intake
    monkeypatch.setattr(reader[0], "read_approved", lambda ws, mode: ("approved", ws, mode))
    monkeypatch.setattr(reader[0], "with_runs", lambda tasks, stranded, facts: tasks)
    monkeypatch.setattr(reader[0], "read_fetch_facts", lambda runs, stranded: {})
    monkeypatch.setattr(reader[1], "read_quarantined", lambda ws, mode: ("quarantined", ws, mode))
    monkeypatch.setattr(reader[2], "read_attempts", lambda ws: ("attempts", ws))
    monkeypatch.setattr(reader[3], "read_intake", lambda ws: ("intake", ws))
    monkeypatch.setattr(reader[3], "read_sources_configured", lambda path: ("sources_configured", path))
    monkeypatch.setattr(cli.chair_read_lease, "read_lease", lambda runs, _now: ("lease", runs))
    monkeypatch.setattr(cli.chair_read_stranded, "read_stranded", lambda *_a: "stranded")
    monkeypatch.setattr(cli, "_chair_stranded_inputs", lambda *_a: ([], []))
    monkeypatch.setattr(cli.chair_read_docket, "read_docket", lambda ws, mode, cap: {"initiatives": [{"id": "i1", "ready_tasks": [1]}], "at": (ws, mode, cap)})
    monkeypatch.setattr(cli.chair_read_live, "read_live_initiatives", lambda runs, names, _now: (runs, names))
    monkeypatch.setattr(cli.chair_read_run_id, "make_run_id", lambda runs: ("run_id", runs))
    deps = cli._chair_run_deps(runs_dir, {}, "chair", 1, "h", True, print, profile_path, "store")
    f = deps.facts_deps
    assert (
        f.approved(), f.quarantined(), f.attempts(), f.intake(), f.sources_configured(), f.lease(), f.stranded(),
        f.docket()["at"], f.live_initiatives(), f.work_store_ready(), deps.exec_deps.run_id,
    ) == (
        ("approved", tmp_path, "store"), ("quarantined", tmp_path, "store"), ("attempts", tmp_path), ("intake", tmp_path),
        ("sources_configured", profile_path), ("lease", runs_dir), "stranded",
        (tmp_path, "store", 3), (runs_dir, ["i1"]), True, ("run_id", runs_dir),
    )


def test_the_recorder_appends_the_action_under_the_fenced_epoch(tmp_path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    deps = cli._chair_run_deps(runs_dir, {}, "chair", 1, "h", True, print, tmp_path / "p.yaml", "files")
    deps.exec_deps.record({"kind": "noop"})
    row = json.loads((runs_dir / "chair.actions.jsonl").read_text(encoding="utf-8"))
    assert (row["kind"], row["epoch"]) == ("noop", -1)


def _workspace_deps(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    return cli._chair_run_deps(runs_dir, {}, "chair", 1, "h", True, print, tmp_path / "p.yaml", "files").exec_deps


def _initiative(tmp_path, name: str, text: str) -> None:
    folder = tmp_path / "work" / name
    folder.mkdir(parents=True)
    (folder / "initiative.md").write_text(text, encoding="utf-8")


def test_repo_for_reads_the_initiatives_repo_when_the_action_has_none(tmp_path) -> None:
    _initiative(tmp_path, "x", "---\nrepo: /w/app\n---\nbody\n")
    exec_deps = _workspace_deps(tmp_path)
    assert exec_deps.repo_for({"kind": "clear_branches", "initiative": "x"}) == "/w/app"
    assert exec_deps.repo_for({"kind": "clear_branches", "initiative": "x", "repo": "/other"}) == "/other"


def test_repo_for_is_empty_when_the_file_or_the_key_is_missing(tmp_path) -> None:
    _initiative(tmp_path, "nokey", "---\ntitle: t\n---\nbody\n")
    exec_deps = _workspace_deps(tmp_path)
    assert exec_deps.repo_for({"kind": "clear_branches", "initiative": "nokey"}) == ""
    assert exec_deps.repo_for({"kind": "clear_branches", "initiative": "absent"}) == ""
    assert exec_deps.repo_for({"kind": "clear_branches"}) == ""


def test_the_real_bundle_runs_cox_from_the_workspace(tmp_path) -> None:
    code, output = _workspace_deps(tmp_path).run(["pwd"])
    assert (code, output.strip()) == (0, str(tmp_path.resolve()))


def test_an_unusable_provider_profile_refuses_before_any_lease(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli, "_leader_runs_dir_or_refuse", lambda _a: ({}, tmp_path / "runs", None))
    monkeypatch.setattr(cli, "_lake_provider", lambda _a: ({}, "provider profile not readable: /x"))
    monkeypatch.setattr(cli.chair, "renew_lease", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("beat ran")))
    monkeypatch.setattr(cli.chair_run, "run", lambda *_a: (_ for _ in ()).throw(AssertionError("loop ran")))
    args = cli.build_parser().parse_args(["chair", "run"])
    assert args.fn(args) == 2
    assert "no lease taken; lake: provider profile not readable: /x" in capsys.readouterr().out


def test_the_loops_lease_take_names_its_label_and_own_pid(monkeypatch, tmp_path) -> None:
    """The loop takes the lease in-process, so the holder is its label and os.getpid(), never the parent pid."""
    monkeypatch.setattr(chair.store_cli, "lease_acquire", lambda _d, _n, holder, _t, _s: store_cli.LeaseGranted(epoch=1, holder=holder))
    deps = cli._chair_run_deps(tmp_path, {}, "lane-a", os.getpid(), "h", False, print, tmp_path / "p.yaml", "files")
    assert deps.exec_deps.acquire_lease("ignored", "h") == ""
    holder = json.loads((tmp_path / chair.LEASE_FILENAME).read_text(encoding="utf-8"))["holder"]
    assert holder == f"lane-a@h:{os.getpid()}"
    assert "unlabeled" not in holder and f":{os.getppid()}" not in holder
