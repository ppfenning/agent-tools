from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_facts, chair_run, cli

# Fields the dataclass carries as plain metadata, not as wired sources: never callable, never unwired.
_FACTS_DEPS_METADATA_FIELDS = frozenset({"session", "pid", "host"})

# `chair_facts.gather_facts` reads every optional field through an `is not None` (or truthy) guard before
# falling back, `queue` and `stranded_records` included (chair_facts.py:300,303,308,322-331) — that guard shape
# is shared and does not by itself distinguish this pair. What distinguishes them is what `_chair_run_deps`
# (agent_tools/cli.py:5747-5776) actually wires: `queue` and `stranded_records` are the only two `FactsDeps`
# keywords it never passes there, so they sit at the dataclass's own `None` default, while every other optional
# field is passed a real callable, e.g. `missing_repos=missing_repos` (line 5760) and
# `history=lambda: chair_read_housekeeping.read_last_housekeeping(runs_dir)` (line 5774). That is a fact about
# what `_chair_run_deps` wires today, not an assumption: if a future change starts passing `queue=` or
# `stranded_records=`, the assertion below fails and names the field to drop from this set.
_FACTS_DEPS_NONE_GUARDED_FIELDS = frozenset({"queue", "stranded_records"})


def test_every_facts_deps_field_the_dataclass_declares_is_wired(tmp_path) -> None:
    """Iterates `FactsDeps`' own fields, so a fact added later is checked with no edit here.

    This replaces a hand-kept set of field names that let `remote_unfetched` and housekeeping ship unwired:
    a name missing from a hand-copied list is a name never checked.
    """
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    for f in dataclasses.fields(chair_facts.FactsDeps):
        value = getattr(deps.facts_deps, f.name)
        if f.name in _FACTS_DEPS_METADATA_FIELDS:
            continue
        if f.name in _FACTS_DEPS_NONE_GUARDED_FIELDS:
            # Checked, not skipped: today's real deps leave these `None`, which `gather_facts` guards for.
            assert value is None, f"{f.name} is no longer None; drop it from _FACTS_DEPS_NONE_GUARDED_FIELDS"
            continue
        assert callable(value), f"{f.name} is not callable: {value!r}"
        assert value is not None, f"{f.name} is None"
        assert not isinstance(value, cli._ChairUnwired), f"{f.name} is unwired: {value!r}"
    # `_ChairUnwired` is one sentinel shared by both bundles: `_chair_unwired_sources` (agent_tools/cli.py:5545-5552)
    # refuses to start while either `deps.facts_deps` or `deps.exec_deps` holds one. The replaced `TWELVE` set
    # named "run_id" and "record", both `exec_deps` fields, so checking `exec_deps` here too keeps that coverage
    # rather than narrowing it to `FactsDeps` alone.
    for f in dataclasses.fields(cli.chair_exec.Deps):
        exec_value = getattr(deps.exec_deps, f.name)
        assert not isinstance(exec_value, cli._ChairUnwired), f"exec_deps.{f.name} is unwired: {exec_value!r}"


def test_tick_calls_plan_with_the_tick_s_own_now() -> None:
    """`tick` takes `now` as its own argument and must hand that exact value to `plan`, not re-derive one."""
    calls: list[tuple] = []

    def fake_plan(facts, now):
        calls.append((facts, now))
        return []

    known_now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    deps = chair_run.RunDeps(
        facts_deps=object(),  # type: ignore[arg-type]
        exec_deps=object(),  # type: ignore[arg-type]
        report_deps=object(),  # type: ignore[arg-type]
        beat=lambda: None,
        current_epoch=lambda: 0,
        holds=lambda: False,
        release=lambda: None,
        sleep=lambda _seconds: None,
        now=lambda: known_now,
        gather=lambda facts_deps, now: {},  # type: ignore[arg-type,return-value]
        plan=fake_plan,
        perform=lambda actions, exec_deps, current_epoch, dry_run: [],
    )

    chair_run.tick(deps, False, known_now)

    assert len(calls) == 1
    assert calls[0][1] == known_now


def test_the_recorder_is_built_with_a_store_runner_and_the_lease_holder(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.chair_read_record, "recorder", lambda *args, **kwargs: seen.update(kwargs))
    cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert seen["holder"] == cli.chair.lease_holder("chair", 1, "h")
    assert callable(seen["store"])


def test_remote_unfetched_history_and_housekeeping_hours_are_wired_not_none(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert callable(deps.facts_deps.remote_unfetched)
    assert callable(deps.facts_deps.history)
    assert callable(deps.facts_deps.housekeeping_hours)


def test_remote_unfetched_is_wired_to_the_docket_s_initiative_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.chair_read_docket, "read_docket", lambda *a, **k: {"initiatives": [{"id": "alpha"}, {"id": "beta"}]})
    seen: dict = {}

    def fake_read(runs_dir, initiatives):
        seen["runs_dir"] = runs_dir
        seen["initiatives"] = list(initiatives)
        return {"alpha": "run-1"}

    monkeypatch.setattr(cli.chair_read_remote_unfetched, "read_remote_unfetched", fake_read)
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.remote_unfetched() == {"alpha": "run-1"}
    assert seen == {"runs_dir": runs, "initiatives": ["alpha", "beta"]}


def test_history_is_wired_to_read_last_housekeeping_at_runs_dir(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_history(runs_dir):
        seen["runs_dir"] = runs_dir
        return "2026-09-27T00:00:00Z"

    monkeypatch.setattr(cli.chair_read_housekeeping, "read_last_housekeeping", fake_history)
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.history() == "2026-09-27T00:00:00Z"
    assert seen == {"runs_dir": runs}


def test_housekeeping_hours_reads_the_profile_s_chair_namespace(tmp_path) -> None:
    runs = tmp_path / "runs"
    profile = {"chair": {"housekeeping_hours": 6}}
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.housekeeping_hours() == 6


def test_housekeeping_hours_is_none_with_no_chair_namespace_in_the_profile(tmp_path) -> None:
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.housekeeping_hours() is None


def test_harness_python_is_the_configured_harness_venv(tmp_path) -> None:
    runs = tmp_path / "runs"
    profile = {"harness_dir": str(tmp_path / "harness")}
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.harness_python == str(Path(profile["harness_dir"]) / ".venv" / "bin" / "python")


def test_harness_python_falls_back_to_the_bare_interpreter_with_no_harness_dir(tmp_path) -> None:
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.harness_python == "python"


def test_the_dispatch_facts_offer_the_lane_hosts_named_in_the_profile_file(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: jarvis\n    ssh: jarvis\n    workspace_dir: /w\n", encoding="utf-8")
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {  # type: ignore[misc]
        "max_in_flight": 3, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 0, "weight": 1, "capabilities": []}],
    }


HOSTS_DDL = ("CREATE TABLE hosts (name TEXT PRIMARY KEY, ssh TEXT, capacity INTEGER, state TEXT, beat_at TEXT,"
             " versions_json TEXT, updated_at TEXT, updated_by TEXT)")
BEAT = '{"login_ok": true, "workspace_dir": "/srv/ws"}'


def _hosts_table(runs, *rows) -> None:
    """The graphs-hosts-table columns; each row is (name, ssh, capacity, state, versions_json)."""
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(HOSTS_DDL)
    conn.executemany(
        "INSERT INTO hosts VALUES (?, ?, ?, ?, '2026-09-26T11:58:00Z', ?, '2026-09-26T11:58:00Z', 'chair')", rows,
    )
    conn.commit()
    conn.close()


def test_the_dispatch_facts_name_only_the_active_table_hosts_when_the_table_has_rows(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: other\n    ssh: other\n    workspace_dir: /w\n", encoding="utf-8")
    runs = tmp_path / "runs"
    _hosts_table(runs, ("jarvis", "jarvis", 8, "active", BEAT), ("pi", "pi", 2, "draining", BEAT))
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {  # type: ignore[misc]
        "max_in_flight": 3, "live_runs": 0,
        "hosts": [{"name": "jarvis", "live_runs": 0, "capacity": 8, "weight": 1, "capabilities": []}],
    }


def test_the_dispatch_facts_carry_weight_and_capabilities_from_the_hosts_row(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    conn.execute(HOSTS_DDL.replace("updated_by TEXT)", "updated_by TEXT, weight INTEGER, capabilities TEXT)"))
    conn.execute(
        "INSERT INTO hosts VALUES ('jarvis', 'jarvis', 8, 'active', '2026-09-26T11:58:00Z', ?, '2026-09-26T11:58:00Z', 'chair', 2, ?)",
        (BEAT, '["go"]'),
    )
    conn.commit()
    conn.close()
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"] == [  # type: ignore[misc]
        {"name": "jarvis", "live_runs": 0, "capacity": 8, "weight": 2, "capabilities": ["go"]}
    ]


def test_an_active_table_host_with_no_workspace_dir_is_not_dispatched_and_the_chair_says_why(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: other\n    ssh: other\n    workspace_dir: /w\n", encoding="utf-8")
    runs = tmp_path / "runs"
    _hosts_table(runs, ("jarvis", "jarvis", 8, "active", BEAT), ("fresh", "fresh", 4, "active", None))
    said: list[str] = []
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, said.append, profile, "files")
    assert [h["name"] for h in deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"]] == ["jarvis"]  # type: ignore[misc]
    assert said == [
        "chair run: not dispatching to fresh: no workspace_dir; run `cox host beat` on each",
        "chair run: profile lane_hosts not in the hosts table are not lane hosts while it has rows: other;"
        " add them with `cox host add`",
    ]


def test_the_dispatch_facts_fall_back_to_the_profile_host_when_the_table_has_no_rows(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: other\n    ssh: other\n    workspace_dir: /w\n", encoding="utf-8")
    runs = tmp_path / "runs"
    _hosts_table(runs)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"] == [  # type: ignore[misc]
        {"name": "other", "live_runs": 0, "weight": 1, "capabilities": []}
    ]


def test_a_live_pidfile_run_with_no_store_lane_still_counts_as_a_local_lane(tmp_path, monkeypatch) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.pid").write_text("123", encoding="utf-8")
    monkeypatch.setattr("agent_tools.epic.run_live", lambda *args, **kwargs: True)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {"max_in_flight": 3, "live_runs": 1, "hosts": []}  # type: ignore[misc]


def test_lanes_on_an_unlisted_host_and_live_pidfiles_all_count_as_local() -> None:
    lanes = [cli.run_store.Lane("r1", host, "t", "t") for host in ("jarvis", "elsewhere", "omarchy")]
    assert cli._dispatch_counts(lanes, "omarchy", ["jarvis"], 2) == {"": 4, "jarvis": 1}


def test_live_lanes_count_per_host_with_the_local_machine_under_the_empty_name() -> None:
    lanes = [cli.run_store.Lane("r1", host, "t", "t") for host in (None, "omarchy", "jarvis")]
    assert cli._live_by_host(lanes, "omarchy") == {"": 2, "jarvis": 1}


def test_the_deps_weekly_reader_passes_a_store_spend_that_reads_the_store_from_since(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *args, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(cli.run_store, "cost_since", lambda runs_dir, since: (runs_dir, since))
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    deps.facts_deps.weekly()
    assert seen["store_spend"]("2026-08-29") == (runs, "2026-08-29")
    assert seen["reset"] is None


def test_the_deps_weekly_reader_passes_the_profiles_parsed_weekly_reset(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.usage_window, "gather_weekly", lambda *args, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(cli.run_store, "cost_since", lambda runs_dir, since: (runs_dir, since))
    runs = tmp_path / "runs"
    profile = {"weekly_reset": "Sun 04:00 America/New_York"}
    deps = cli._chair_run_deps(runs, profile, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    deps.facts_deps.weekly()
    assert seen["reset"] == cli.usage_window.parse_weekly_reset("Sun 04:00 America/New_York")


class _FakeStore:
    """A store whose lease row names one holder; each renewal by that holder moves the heartbeat counter."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        self.heartbeat = 0

    def renew(self, holder: str, epoch: int):
        if holder != self.holder:
            return cli.chair.store_cli.LeaseRefused(epoch + 1, self.holder)
        self.heartbeat += 1
        return cli.chair.store_cli.LeaseGranted(epoch, holder)


_OLD = "2020-01-01T00:00:00+00:00"


def _chair_on_disk(tmp_path, monkeypatch, store_holder: str):
    """A record file with an old heartbeat, a lease sidecar for chair-x@h:1, and deps whose tick stops after the beat."""
    runs = tmp_path / "runs"
    runs.mkdir()
    store = _FakeStore(store_holder)
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: store.renew(who, epoch))
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    cli.chair.write(runs, {"session": "chair-x", "pid": 1, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD, "runs": [], "claude_session": None})
    deps = cli._chair_run_deps(runs, {}, "chair-x", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    def stop_after_beat(*_args):
        raise RuntimeError("beat done")

    quiet = dataclasses.replace(deps.report_deps, echo=lambda line: None)
    return runs, store, dataclasses.replace(deps, gather=stop_after_beat, report_deps=quiet)


def test_one_loop_tick_moves_the_store_lease_heartbeat_and_the_record_file_heartbeat(tmp_path, monkeypatch) -> None:
    runs, store, deps = _chair_on_disk(tmp_path, monkeypatch, "chair-x@h:1")

    chair_run.run(True, 0, False, deps)

    assert store.heartbeat == 1
    assert cli.chair.read(runs)["heartbeat_at"] > _OLD


def test_a_tick_that_lost_the_lease_leaves_the_record_file_untouched(tmp_path, monkeypatch) -> None:
    runs, store, deps = _chair_on_disk(tmp_path, monkeypatch, "other@h:9")
    before = cli.chair.chair_path(runs).read_bytes()

    chair_run.run(True, 0, False, deps)

    assert store.heartbeat == 0
    assert cli.chair.chair_path(runs).read_bytes() == before


def test_a_beat_during_an_active_takeover_until_still_advances_the_record_file_heartbeat(tmp_path, monkeypatch) -> None:
    """The lease sidecar already names this loop (chair-x@h:1) -- reclaimed, e.g., by a take_lease action after an
    expired takeover -- but the record file still names the prior holder, with a takeover `until` still ahead.
    A tick's beat must still move heartbeat_at forward for the holder the lease actually names."""
    runs = tmp_path / "runs"
    runs.mkdir()
    store = _FakeStore("chair-x@h:1")
    monkeypatch.setattr(cli.chair.store_cli, "lease_renew", lambda runs_dir, name, who, epoch, ttl: store.renew(who, epoch))
    (runs / cli.chair.LEASE_FILENAME).write_text(json.dumps({"holder": "chair-x@h:1", "epoch": 3}), encoding="utf-8")
    cli.chair.write(runs, {
        "session": "alice", "pid": 9, "host": "h", "taken_at": _OLD, "heartbeat_at": _OLD,
        "runs": [], "claude_session": None, "until": "2099-01-01T00:00:00+00:00",
    })
    deps = cli._chair_run_deps(runs, {}, "chair-x", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    def stop_after_beat(*_args):
        raise RuntimeError("beat done")

    quiet = dataclasses.replace(deps.report_deps, echo=lambda line: None)
    deps = dataclasses.replace(deps, gather=stop_after_beat, report_deps=quiet)

    chair_run.run(True, 0, False, deps)

    assert store.heartbeat == 1
    assert cli.chair.read(runs)["heartbeat_at"] > _OLD


def _stranded_workspace_with_a_missing_repo(tmp_path):
    """A `runs`/`work` pair whose one live, approved, unlanded record names a repo that does not exist."""
    runs = tmp_path / "runs"
    missing_repo = str(tmp_path / "gone")
    tasks = runs / "r1" / "tasks" / "p1"
    tasks.mkdir(parents=True)
    record = {
        "run": "r1", "task": "t1", "phase": "p1", "branch": "b1",
        "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "landed": False, "repo": missing_repo,
    }
    (tasks / "t1.json").write_text(json.dumps(record), encoding="utf-8")
    work = tmp_path / "work" / "acme" / "p1"
    work.mkdir(parents=True)
    (work / "t1.md").write_text("---\nid: t1\nstate: ready\n---\nbody\n", encoding="utf-8")
    (tmp_path / "work" / "acme" / "initiative.md").write_text("---\nrepo: /unused\n---\nbody\n", encoding="utf-8")
    return runs, missing_repo


def test_a_missing_repo_is_reported_on_the_first_tick_and_not_the_second(tmp_path) -> None:
    runs, missing_repo = _stranded_workspace_with_a_missing_repo(tmp_path)
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")

    first_missing = deps.facts_deps.missing_repos()  # type: ignore[misc]
    first_reported = deps.facts_deps.reported_repos()  # type: ignore[misc]
    assert cli.chair_facts.new_missing_repos(first_missing, first_reported) == [missing_repo]

    second_missing = deps.facts_deps.missing_repos()  # type: ignore[misc]
    second_reported = deps.facts_deps.reported_repos()  # type: ignore[misc]
    assert cli.chair_facts.new_missing_repos(second_missing, second_reported) == []


def test_the_record_file_is_beaten_only_by_a_live_run_with_no_refusal() -> None:
    assert [
        cli._record_beat_wanted(False, ""),
        cli._record_beat_wanted(False, "chair: held by other@h:9 (store lease)"),
        cli._record_beat_wanted(True, ""),
    ] == [True, False, False]


def test_the_real_deps_read_run_exits_from_the_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli.chair_read_exits, "exits", lambda runs_dir: {"i": True, "runs": str(runs_dir)})
    runs = tmp_path / "runs"
    deps = cli._chair_run_deps(runs, {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.facts_deps.run_exited() == {"i": True, "runs": str(runs)}  # type: ignore[misc]
