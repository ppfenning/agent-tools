from __future__ import annotations

import dataclasses
import json
import sqlite3

from agent_tools import chair_run, cli

TWELVE = {
    "lease", "docket", "approved", "quarantined", "stranded", "attempts",
    "live_initiatives", "intake", "work_store_ready", "sources_configured", "run_id", "record",
}


def test_the_real_deps_hold_no_unwired_source_for_any_of_the_twelve_names(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    fields = {
        f.name: getattr(bundle, f.name)
        for bundle in (deps.facts_deps, deps.exec_deps)
        for f in dataclasses.fields(bundle)
    }
    assert fields.keys() >= TWELVE
    assert [name for name in TWELVE if isinstance(fields[name], cli._ChairUnwired)] == []


def test_the_recorder_is_built_with_a_store_runner_and_the_lease_holder(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli.chair_read_record, "recorder", lambda *args, **kwargs: seen.update(kwargs))
    cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert seen["holder"] == cli.chair.lease_holder("chair", 1, "h")
    assert callable(seen["store"])


def test_the_dispatch_facts_offer_the_lane_hosts_named_in_the_profile_file(tmp_path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("lane_hosts:\n  - name: jarvis\n    ssh: jarvis\n    workspace_dir: /w\n", encoding="utf-8")
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.dispatch({"max_in_flight": 3}) == {  # type: ignore[misc]
        "max_in_flight": 3, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 0}],
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
        "max_in_flight": 3, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 0, "capacity": 8}],
    }


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
    assert deps.facts_deps.dispatch({"max_in_flight": 3})["hosts"] == [{"name": "other", "live_runs": 0}]  # type: ignore[misc]


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
