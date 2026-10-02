import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
import yaml
from test_run_store import ROW, leases_table, store

from agent_tools import leader, pacing, route, run_store, usage_window
from agent_tools.cli import main
from agent_tools.pacing import Window

# Fixed in the past so `_usage_assessment`'s own `datetime.now(UTC)` always
# falls after `_USAGE_END`, pinning `elapsed_fraction` regardless of when the
# test runs (same trick as tests/test_usage_assess_cli.py).
_USAGE_START = datetime.datetime(2020, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)
_USAGE_END = datetime.datetime(2020, 1, 1, 10, 0, 0, tzinfo=datetime.UTC)


def _unmeasured_window(*_a, **_k) -> Window:
    return Window(start=_USAGE_START, end=_USAGE_END, spent_usd=0.0, ceiling_usd=None,
                  burn_usd_per_hour=0.0, runs_in_flight=0)


@pytest.fixture(autouse=True)
def _no_gh_sync_on_file(monkeypatch):
    """`route file` syncs each item it writes; nothing in this file may reach a real `gh`."""
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _stub_usage_gather(monkeypatch):
    """Every route context/launch test in this file crosses `_usage_assessment`;
    stub the gatherer to an unmeasured window so no test shells out to `npx
    ccusage`. A test that wants a different verdict re-patches `gather` itself."""
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


def test_context_text_with_no_profile_mentions_no_profile(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "context", "--profile", str(missing)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no profile" in out
    assert out.count("\n") == 1  # one line, no counts


def test_context_json_with_no_profile_has_reason_and_empty_lists(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "context", "--profile", str(missing), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "no profile" in doc["reason"]
    assert doc["intake"] == [] and doc["runs"] == [] and doc["initiatives"] == []
    assert doc["team"] is None


def test_context_text_with_profile_missing_workspace_dir_has_reason_on_first_line(tmp_path, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: acme\n")
    rc = main(["route", "context", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    first_line = out.splitlines()[0]
    assert "workspace_dir not set in profile" in first_line


def test_context_json_with_profile_missing_workspace_dir_has_reason_and_empty_lists(tmp_path, capsys):
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: acme\n")
    rc = main(["route", "context", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["reason"] == "workspace_dir not set in profile"
    assert doc["intake"] == [] and doc["runs"] == [] and doc["initiatives"] == []
    assert doc["team"] == "acme"


def _write_workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "intake").mkdir(parents=True)
    (ws / "runs").mkdir(parents=True)
    (ws / "work" / "demo" / "1-build").mkdir(parents=True)
    (ws / "intake" / "one.md").write_text("---\nid: i1\ntitle: Do the thing\n---\n\nBody\n")
    (ws / "runs" / "run1.pid").write_text(str(os.getpid()))
    (ws / "runs" / "run2.pid").write_text("garbage")
    (ws / "work" / "demo" / "1-build" / "task.md").write_text("---\nstate: ready\n---\n\nDo the task\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    return profile


def test_context_json_with_full_profile_lists_initiative_and_runs(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    rc = main(["route", "context", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["initiatives"] == [{"id": "demo", "phase": "1-build", "ready": 1}]
    runs = {row["id"]: row for row in doc["runs"]}
    assert len(runs) == 2
    assert runs["run1"]["pid"] == os.getpid() and runs["run1"]["alive"] is True
    assert runs["run2"]["pid"] is None and runs["run2"]["alive"] is False
    assert doc["intake"] == {
        "queued": [{"id": "i1", "title": "Do the thing", "initiative": None, "done": False, "path": "intake/one.md"}],
        "decomposed": [],
        "landed": [],
    }


def test_context_reports_an_unknown_state_as_a_problem_under_its_initiative(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "work" / "demo" / "1-build" / "task.md").write_text("---\nstate: superseded\n---\n\nDo the task\n")
    rc = main(["route", "context", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["initiatives"] == [{"id": "demo", "phase": None, "ready": 0}]
    assert doc["problems"] == ["demo: 1-build/task.md: unknown state 'superseded'"]

    rc = main(["route", "context", "--profile", str(profile)])
    out = capsys.readouterr().out
    lines = out.splitlines()
    ready_line = next(i for i, line in enumerate(lines) if line.startswith("ready:"))
    assert lines[ready_line] == "ready: demo (unlaunchable)"
    assert lines[ready_line + 1] == "problem: demo: 1-build/task.md: unknown state 'superseded'"


def test_status_json_without_an_intake_dir_still_carries_problems(tmp_path, capsys):
    """The validator's finding on run 1: the bare-rows branch dropped the problem list."""
    profile = _write_workspace(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "work" / "demo" / "1-build" / "task.md").write_text("---\nstate: superseded\n---\n\nDo the task\n")
    rc = main(["route", "status", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert isinstance(doc["runs"], list)
    assert doc["problems"] == ["demo: 1-build/task.md: unknown state 'superseded'"]


def test_context_carries_the_heartbeat_of_a_run_holding_its_lease_and_none_for_the_rest(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    leases_table(tmp_path / "workspace" / "runs", ("runs:run1", "run1", "2999-01-01T00:00:00Z", "2026-09-25T05:59:48Z"), ("runs:run2", "run1", "2999-01-01T00:00:00Z", "2026-09-25T05:59:48Z"))
    assert main(["route", "context", "--profile", str(profile), "--json"]) == 0
    runs = {row["id"]: row for row in json.loads(capsys.readouterr().out)["runs"]}
    assert runs["run1"]["heartbeat"] == "2026-09-25T05:59:48Z"
    assert runs["run2"]["heartbeat"] is None
    assert main(["route", "context", "--profile", str(profile)]) == 0
    lanes = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("lanes:"))
    assert "run1 (since " in lanes and ", heartbeat " in lanes and "pid" not in lanes


def _efficiency_line(capsys):
    return next((l for l in capsys.readouterr().out.splitlines() if l.startswith("efficiency:")), None)


def _today_and_calls(*calls):
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    return ts, [{**ROW, "call_id": f"c{i}", "ts": ts, **call} for i, call in enumerate(calls)]


def _landed(ts, run, task):
    return json.dumps({"ts": ts, "run": run, "task": task, "steps_reached": ["mark_done"], "exit": 0, "pr": None})


def test_context_prints_todays_spend_lands_and_first_try_rate_after_lanes(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    runs = tmp_path / "workspace" / "runs"
    # Ids as the harness writes them: land.jsonl names the bare ticket, node_calls a truncated `run:phase:ticket` composite.
    ts, calls = _today_and_calls(
        {"cost_usd": 1.5, "role": "build", "task_id": "epic-x-5:seams:seams-t"},
        {"cost_usd": 1.5, "role": "build", "task_id": "epic-x-5:wires:wires-t"},
        {"cost_usd": 3.0, "role": "build", "task_id": "epic-x-5:wires:wires-t"},
    )
    store(runs, *calls)
    lines = [_landed(ts, "epic-x-5", "seams-task"), "not json", _landed(ts, "epic-x-5", "wires-task"), _landed(ts, "epic-x-5", "loose-task")]
    (runs / "land.jsonl").write_text("\n".join(lines) + "\n")
    assert main(["route", "context", "--profile", str(profile)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[out.index(next(l for l in out if l.startswith("lanes:"))) + 1] == (
        "efficiency: today $6.00 for 3 landed ($2.00 per landed task), first-try 50% of 2 measured"
    )


def test_context_does_not_hand_a_ticket_the_builds_of_a_ticket_whose_name_it_extends(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    runs = tmp_path / "workspace" / "runs"
    ts, calls = _today_and_calls(
        {"cost_usd": 1.0, "role": "build", "task_id": "epic-x-5:seams:seams-task"},
        {"cost_usd": 1.0, "role": "build", "task_id": "epic-x-5:seams:seams-task-two"},
        {"cost_usd": 1.0, "role": "build", "task_id": "epic-x-5:seams:seams-task-two"},
    )
    store(runs, *calls)
    (runs / "land.jsonl").write_text("\n".join([_landed(ts, "epic-x-5", "seams-task"), _landed(ts, "epic-x-5", "seams-task-two")]) + "\n")
    assert main(["route", "context", "--profile", str(profile)]) == 0
    assert _efficiency_line(capsys) == "efficiency: today $3.00 for 2 landed ($1.50 per landed task), first-try 0% of 1 measured"


def test_context_says_nothing_landed_yet_when_the_store_has_spend_and_no_lands(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    store(tmp_path / "workspace" / "runs", *_today_and_calls({"cost_usd": 0.5})[1])
    assert main(["route", "context", "--profile", str(profile)]) == 0
    assert _efficiency_line(capsys) == "efficiency: today $0.50, nothing landed yet"


def test_context_prints_no_efficiency_line_with_no_store(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    assert main(["route", "context", "--profile", str(profile)]) == 0
    assert _efficiency_line(capsys) is None


@pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"), ValueError("bad cost"), TypeError("bad row")])
def test_context_prints_no_efficiency_line_and_still_the_docket_when_the_store_errors(tmp_path, capsys, monkeypatch, error):
    profile = _write_workspace(tmp_path)

    def broken(*_a, **_k):
        raise error

    monkeypatch.setattr(run_store, "cost_since", broken)
    assert main(["route", "context", "--profile", str(profile)]) == 0
    out = capsys.readouterr().out
    assert "efficiency:" not in out and "lanes: 1 busy" in out


def test_context_text_with_full_profile_lists_initiative_and_runs(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    rc = main(["route", "context", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo" in out
    assert "intake: 1 queued, 0 decomposed, 0 landed" in out
    assert "lanes: 1 busy" in out
    assert "run1" in out and "run2" not in out


def test_context_text_with_full_profile_ends_with_the_usage_reason_line(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    rc = main(["route", "context", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[-1] == "usage: window is unmeasured: no usable ceiling_usd; reporting pace only"


def test_context_text_with_full_profile_prints_the_resolved_gate_level(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    rc = main(["route", "context", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[-2] == "gate: ticket"


def test_context_json_with_full_profile_carries_the_same_usage_reason(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    rc = main(["route", "context", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["usage"] == "window is unmeasured: no usable ceiling_usd; reporting pace only"


def test_context_threads_the_profiles_spend_window_ceiling_into_gather(tmp_path, monkeypatch, capsys):
    """`_stub_usage_gather` above is replaced here, per its own docstring, so
    this test can see what `ceiling_usd` the profile's `spend:` block
    actually reaches `gather` with, rather than the block being read at all."""
    profile = _write_workspace(tmp_path)
    profile.write_text(profile.read_text() + "spend:\n  window_ceiling_usd: 42\n")
    captured = {}

    def fake_gather(runs_dir, now, ceiling_usd=None, usage=None):
        captured["ceiling_usd"] = ceiling_usd
        return _unmeasured_window(runs_dir, now)

    monkeypatch.setattr(usage_window, "gather", fake_gather)
    rc = main(["route", "context", "--profile", str(profile)])
    assert rc == 0
    assert captured["ceiling_usd"] == 42.0


def test_context_with_work_item_missing_frontmatter_still_exits_zero_and_not_ready(tmp_path, capsys):
    profile = _write_workspace(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "work" / "demo" / "1-build" / "task.md").write_text("Just some task notes, no frontmatter at all.\n")
    rc = main(["route", "context", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["initiatives"] == []


_LOG_RUN3 = """
  quarantined task: c — timeout
  reused d from run-9 (approved patch, no model call)
epic run-3: 1 phase(s) complete, 0 partial, 1 blocked, 1 task(s) quarantined, 0 stack(s) rebased
  usage   : 3 node call(s), 12 turns, $0.40
"""


def _write_runs_workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / "run1.pid").write_text(str(os.getpid()))  # alive: the test process itself
    (ws / "runs" / "run2.pid").write_text("999999999")  # dead: pattern from test_epic_and_cli.py
    (ws / "runs" / "run3.log").write_text(_LOG_RUN3)  # no pidfile at all
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    return profile, ws


def _mtime_utc(path):
    # Independent of agent_tools.cli._mtime_iso: reimplemented here so a bug
    # in that function (say, local time instead of UTC) shows up as a
    # mismatch instead of passing on both sides of the same helper.
    import datetime

    return datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.UTC).isoformat()


def _expected_status_rows(ws):
    from agent_tools import epic

    entries = [
        {"id": "run1", "pid": os.getpid(), "alive": True,
         "started": _mtime_utc(ws / "runs" / "run1.pid"), **epic.summarize_log("")},
        {"id": "run2", "pid": 999999999, "alive": False,
         "started": _mtime_utc(ws / "runs" / "run2.pid"), **epic.summarize_log("")},
        {"id": "run3", "pid": None, "alive": False, "started": None, **epic.summarize_log(_LOG_RUN3)},
    ]
    return [{**row, "host": None, "remote": False} for row in route.status_rows(entries)]


def test_status_text_no_profile_exits_2_and_names_the_path(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "status", "--profile", str(missing)])
    out = capsys.readouterr().out
    assert rc == 2
    assert str(missing) in out


def test_status_json_no_profile_exits_2_and_names_the_path(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "status", "--profile", str(missing), "--json"])
    out = capsys.readouterr().out
    assert rc == 2
    assert str(missing) in out


def test_status_json_with_profile_matches_status_rows(tmp_path, capsys):
    profile, ws = _write_runs_workspace(tmp_path)
    rc = main(["route", "status", "--profile", str(profile), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert rows == _expected_status_rows(ws)


def test_status_json_with_an_intake_dir_carries_runs_and_intake_groups(tmp_path, capsys):
    profile, ws = _write_runs_workspace(tmp_path)
    (ws / "intake").mkdir()
    (ws / "intake" / "2026-09-01-q.md").write_text("---\nid: q\ntitle: Q\n---\n\nBody.\n")
    rc = main(["route", "status", "--profile", str(profile), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["runs"] == _expected_status_rows(ws)
    assert doc["intake"] == {
        "queued": [{"id": "q", "title": "Q", "initiative": None, "done": False, "path": "intake/2026-09-01-q.md"}],
        "decomposed": [],
        "landed": [],
    }


def test_status_text_hides_the_old_no_pidfile_run_and_says_so(tmp_path, capsys):
    profile, ws = _write_runs_workspace(tmp_path)
    rc = main(["route", "status", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == route.render_status(_expected_status_rows(ws)[:2], gate_level="ticket", hidden=1) + "\n"
    assert out.splitlines()[2] == "(1 older runs hidden; --all shows every run)"


def test_status_text_all_lists_alive_dead_and_no_pidfile_runs(tmp_path, capsys):
    profile, ws = _write_runs_workspace(tmp_path)
    rc = main(["route", "status", "--profile", str(profile), "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == route.render_status(_expected_status_rows(ws), gate_level="ticket") + "\n"
    assert "hidden" not in out


def test_status_json_returns_every_row_without_all(tmp_path, capsys):
    profile, ws = _write_runs_workspace(tmp_path)
    rc = main(["route", "status", "--profile", str(profile), "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [r["id"] for r in rows] == ["run1", "run2", "run3"]


def test_status_json_treats_pid_zero_as_not_alive_and_survives_an_oversized_pidfile(tmp_path, capsys):
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / "run-zero.pid").write_text("0")  # pid 0 signals the whole process group, not a run
    (ws / "runs" / "run-huge.pid").write_text("9" * 40)  # too big for os.kill's pid_t
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    rc = main(["route", "status", "--profile", str(profile), "--json"])
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)}
    assert rc == 0
    assert rows["run-zero"]["pid"] == 0 and rows["run-zero"]["state"] == "exited"
    assert rows["run-huge"]["state"] == "exited"


_BEAT = "2026-09-25T11:59:48Z"
_LIVE = "2999-01-01T00:00:00Z"  # `epic.run_live` reads the real clock, so a live lease must outlast it


def _shared_store(runs_dir, *leases, runs=(("far-1", "host-b"),), host_column=True):
    """A store as a shared backend holds it: run rows, and lease rows of (name, holder, expires_at, heartbeat_at)."""
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, launched_at TEXT, host TEXT)")
    conn.executemany("INSERT INTO runs VALUES (?, '2026-09-25T10:40:00Z', ?)", runs)
    if not host_column:
        conn.execute("ALTER TABLE runs DROP COLUMN host")
    conn.commit()
    conn.close()
    leases_table(runs_dir, *leases)


def _remote_lane_text():
    since = datetime.datetime.fromisoformat("2026-09-25T10:40:00Z").astimezone().strftime("%H:%M")
    return f"far-1 (on host-b, since {since}, heartbeat 12s ago)"


@pytest.fixture
def pinned_now(monkeypatch):
    """The wall clock beneath `cli._now_iso`, frozen at 12:00:00.5 UTC. A lease time is text, so only a `now` with no fraction and a `Z` suffix compares right against it."""

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 25, 12, 0, 0, 500000, tzinfo=tz)

    monkeypatch.setattr("agent_tools.cli.datetime", types.SimpleNamespace(**{**vars(datetime), "datetime": Frozen}))


def test_the_docket_lists_a_live_lease_with_no_local_pidfile_as_a_remote_lane(tmp_path, capsys, pinned_now):
    profile = _write_workspace(tmp_path)
    _shared_store(tmp_path / "workspace" / "runs", ("runs:far", "far-1", _LIVE, _BEAT))
    assert main(["route", "context", "--profile", str(profile)]) == 0
    lanes = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("lanes:"))
    assert lanes.endswith(f", {_remote_lane_text()}") and lanes.startswith("lanes: 2 busy")
    assert main(["route", "context", "--profile", str(profile), "--json"]) == 0
    runs = {row["id"]: row for row in json.loads(capsys.readouterr().out)["runs"]}
    assert (runs["far-1"]["host"], runs["far-1"]["remote"], runs["far-1"]["pid"]) == ("host-b", True, None)
    assert (runs["run1"]["host"], runs["run1"]["remote"]) == (None, False)


def test_status_lists_a_live_lease_with_no_local_pidfile_as_a_remote_lane(tmp_path, capsys, pinned_now):
    profile = _write_workspace(tmp_path)
    _shared_store(tmp_path / "workspace" / "runs", ("runs:far", "far-1", _LIVE, _BEAT))
    assert main(["route", "status", "--profile", str(profile)]) == 0
    assert f"far-1: alive {_remote_lane_text().removeprefix('far-1 ')}" in capsys.readouterr().out.splitlines()
    assert main(["route", "status", "--profile", str(profile), "--json"]) == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["runs"]}
    assert (rows["far-1"]["host"], rows["far-1"]["remote"], rows["far-1"]["state"]) == ("host-b", True, "alive")
    assert (rows["run1"]["host"], rows["run1"]["remote"]) == (None, False)


def test_a_run_with_a_local_pidfile_and_a_live_lease_shows_once_as_local(tmp_path, capsys, pinned_now):
    profile = _write_workspace(tmp_path)
    _shared_store(tmp_path / "workspace" / "runs", ("runs:run1", "run1", _LIVE, _BEAT), runs=(("run1", "host-b"),))
    assert main(["route", "context", "--profile", str(profile), "--json"]) == 0
    runs = [row for row in json.loads(capsys.readouterr().out)["runs"] if row["id"] == "run1"]
    assert [(row["host"], row["remote"], row["pid"]) for row in runs] == [(None, False, os.getpid())]
    assert main(["route", "status", "--profile", str(profile), "--json"]) == 0
    rows = [row for row in json.loads(capsys.readouterr().out)["runs"] if row["id"] == "run1"]
    assert [(row["host"], row["remote"], row["state"]) for row in rows] == [(None, False, "alive")]
    assert main(["route", "context", "--profile", str(profile)]) == 0
    lanes = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("lanes:"))
    assert lanes.count("run1 (") == 1 and "on host-b" not in lanes


def test_a_lease_that_expired_a_second_ago_is_not_listed(tmp_path, capsys, pinned_now):
    """At 12:00:00.5Z the lease that ended at 11:59:59Z and the one that ended at 12:00:00Z are both gone; only the one ending at 12:00:01Z is live."""
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    _shared_store(
        ws / "runs",
        ("runs:old", "old-1", "2026-09-25T11:59:59Z", _BEAT),
        ("runs:edge", "edge-1", "2026-09-25T12:00:00Z", _BEAT),
        ("runs:live", "live-1", "2026-09-25T12:00:01Z", _BEAT),
        runs=(("old-1", "host-b"), ("edge-1", "host-b"), ("live-1", "host-b")),
    )
    assert main(["route", "status", "--profile", str(profile), "--json"]) == 0
    assert [row["id"] for row in json.loads(capsys.readouterr().out)] == ["live-1"]


def test_on_a_sqlite_store_holding_only_local_leases_the_output_is_the_local_only_output(tmp_path, capsys, monkeypatch, pinned_now):
    profile, ws = _write_runs_workspace(tmp_path)
    _shared_store(ws / "runs", ("runs:run1", "run1", _LIVE, _BEAT), runs=(("run1", None),), host_column=False)
    argvs = (["route", "context", "--profile", str(profile)], ["route", "status", "--profile", str(profile), "--all"], ["route", "status", "--profile", str(profile), "--json"])

    def outputs():
        result = []
        for argv in argvs:
            assert main(argv) == 0
            result.append(capsys.readouterr().out)
        return result

    with monkeypatch.context() as local_only:
        local_only.setattr(run_store, "live_lanes", lambda *_a: [])
        expected = outputs()
    docket, status_text, status_json = actual = outputs()
    assert actual == expected
    since = datetime.datetime.fromtimestamp((ws / "runs" / "run1.pid").stat().st_mtime, datetime.UTC).astimezone().strftime("%H:%M")
    assert f"lanes: 1 busy — run1 (since {since}, heartbeat 12s ago)" in docket.splitlines()
    assert status_text.splitlines()[:2] == [
        f"run1: alive (pid {os.getpid()}, started {_mtime_utc(ws / 'runs' / 'run1.pid')})",
        f"run2: exited (pid 999999999, started {_mtime_utc(ws / 'runs' / 'run2.pid')})",
    ]
    assert status_text.splitlines()[2:] == [
        "run3: no pidfile quarantined task: c — timeout reused d from run-9 (approved patch, no model call)"
        " epic run-3: 1 phase(s) complete, 0 partial, 1 blocked, 1 task(s) quarantined, 0 stack(s) rebased"
        " usage   : 3 node call(s), 12 turns, $0.40",
        "gate: ticket",
    ]
    rows = json.loads(status_json)
    assert [(row["host"], row["remote"]) for row in rows] == [(None, False)] * 3
    assert rows == _expected_status_rows(ws)


def _write_file_profile(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\n"
        "provider_profile: /opt/providers/acme.yaml\n"
    )
    return profile, ws


def test_file_writes_initiative_files_at_expected_paths(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    rc = main(["route", "file", "--profile", str(profile), "--repo", "/repos/widget", "--title", "Fix the thing"])
    out = capsys.readouterr().out
    assert rc == 0
    expected = route.initiative_files("Fix the thing", "", "/repos/widget")
    for rel, text in expected.items():
        assert (ws / rel).read_text(encoding="utf-8") == text
        assert str(ws / rel) in out


def test_file_intake_writes_the_intake_file_with_todays_date(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    expected_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    rc = main(["route", "file", "--profile", str(profile), "--repo", "/repos/widget", "--title", "Fix the thing", "--intake"])
    out = capsys.readouterr().out
    assert rc == 0
    written = list((ws / "intake").glob("*.md"))
    assert len(written) == 1
    assert written[0].name.startswith(expected_date)
    expected = route.intake_file("Fix the thing", "", "/repos/widget", expected_date)
    [(rel, text)] = expected.items()
    assert (ws / rel).read_text(encoding="utf-8") == text
    assert str(ws / rel) in out


def test_file_intake_with_ids_sequence_writes_the_allocated_id(tmp_path, monkeypatch, capsys):
    profile, ws = _write_file_profile(tmp_path)
    with profile.open("a") as f:
        f.write("ids: sequence\n")
    monkeypatch.setattr(route, "allocate_intake_id", lambda: "I7")
    rc = main(["route", "file", "--profile", str(profile), "--repo", "/repos/widget", "--title", "Fix the thing", "--intake"])
    capsys.readouterr()
    assert rc == 0
    written = list((ws / "intake").glob("*.md"))
    assert [p.name for p in written] == ["I7.md"]
    fields, _ = route.parse_frontmatter(written[0].read_text(encoding="utf-8"))
    assert fields == {"id": "I7", "title": "Fix the thing"}


def test_file_refuses_an_existing_path_collision(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    slug = route.slugify("Fix the thing")
    target = ws / "work" / slug / "initiative.md"
    target.parent.mkdir(parents=True)
    target.write_text("already here\n")
    rc = main(["route", "file", "--profile", str(profile), "--repo", "/repos/widget", "--title", "Fix the thing"])
    out = capsys.readouterr().out
    assert rc == 2
    assert str(target) in out
    assert target.read_text() == "already here\n"


def test_file_refuses_a_missing_profile(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "file", "--profile", str(missing), "--repo", "/repos/widget", "--title", "Fix the thing"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no profile" in out


def test_file_from_intake_links_both_ways_retires_the_file_and_status_reports_decomposed(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    (ws / "intake").mkdir()
    intake_path = ws / "intake" / "2026-09-01-fix-thing.md"
    intake_path.write_text("---\nid: fix-thing\ntitle: Fix the thing\nrepo: /repos/widget\n---\n\nDo it.\n")
    rc = main(["route", "file", "--profile", str(profile), "--from-intake", str(intake_path)])
    capsys.readouterr()
    assert rc == 0
    assert not intake_path.exists()
    done_path = ws / "intake" / "done" / "2026-09-01-fix-thing.md"
    assert done_path.exists()
    assert route.parse_frontmatter(done_path.read_text())[0]["initiative"] == route.slugify("Fix the thing")
    slug = route.slugify("Fix the thing")
    initiative_text = (ws / "work" / slug / "initiative.md").read_text()
    assert route.parse_frontmatter(initiative_text)[0]["intake"] == "intake/2026-09-01-fix-thing.md"
    rc = main(["route", "status", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "intake decomposed: 1" in out


def test_file_from_intake_writes_surfaces_from_real_backticked_files_under_repo(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    repo = tmp_path / "repos" / "widget"
    (repo / "agent_tools").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "agent_tools" / "schema.py").write_text("x")
    (repo / "tests" / "test_release.py").write_text("x")
    (ws / "intake").mkdir()
    intake_path = ws / "intake" / "2026-09-01-fix-thing.md"
    intake_path.write_text(
        f"---\nid: fix-thing\ntitle: Fix the thing\nrepo: {repo}\n---\n\n"
        "Touch `agent_tools/schema.py` and `tests/test_release.py`, not `agent_tools/missing.py`.\n"
    )
    rc = main(["route", "file", "--profile", str(profile), "--from-intake", str(intake_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "surfaces: agent_tools/schema.py, tests/test_release.py" in out
    slug = route.slugify("Fix the thing")
    task_text = (ws / "work" / slug / "build" / f"{slug}.md").read_text()
    assert "surfaces: [agent_tools/schema.py, tests/test_release.py]" in task_text
    assert "budget_usd: 2.0" in task_text
    assert "attempts: []" in task_text
    assert "lint: []" in task_text


def test_file_from_intake_reports_no_surfaces_when_no_backticked_token_resolves(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    (ws / "intake").mkdir()
    intake_path = ws / "intake" / "2026-09-01-fix-thing.md"
    intake_path.write_text(
        "---\nid: fix-thing\ntitle: Fix the thing\nrepo: /repos/widget\n---\n\n"
        "See `agent_tools/missing.py` for context.\n"
    )
    rc = main(["route", "file", "--profile", str(profile), "--from-intake", str(intake_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "surfaces: none resolved from the intake body" in out
    slug = route.slugify("Fix the thing")
    task_text = (ws / "work" / slug / "build" / f"{slug}.md").read_text()
    assert "surfaces: []" in task_text


def test_file_from_intake_uses_the_slug_field_over_the_title(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    (ws / "intake").mkdir()
    intake_path = ws / "intake" / "2026-09-01-fix-thing.md"
    intake_path.write_text(
        "---\nid: fix-thing\ntitle: Fix the thing\nrepo: /repos/widget\nslug: named-slug\n---\n\nDo it.\n"
    )
    rc = main(["route", "file", "--profile", str(profile), "--from-intake", str(intake_path)])
    capsys.readouterr()
    assert rc == 0
    assert (ws / "work" / "named-slug" / "build" / "named-slug.md").exists()


def test_file_from_intake_refuses_a_path_outside_the_workspace_intake_dir(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("---\nid: x\ntitle: X\nrepo: /repos/widget\n---\n\nBody.\n")
    rc = main(["route", "file", "--profile", str(profile), "--from-intake", str(outside)])
    out = capsys.readouterr().out
    assert rc == 2
    assert outside.exists()
    assert "must name a file directly under" in out


def test_status_reports_landed_once_the_linked_initiatives_one_task_is_done(tmp_path, capsys):
    profile, ws = _write_file_profile(tmp_path)
    (ws / "intake").mkdir()
    intake_path = ws / "intake" / "2026-09-01-fix-thing.md"
    intake_path.write_text("---\nid: fix-thing\ntitle: Fix the thing\nrepo: /repos/widget\n---\n\nDo it.\n")
    rc = main(["route", "file", "--profile", str(profile), "--from-intake", str(intake_path)])
    capsys.readouterr()
    assert rc == 0
    slug = route.slugify("Fix the thing")
    task_path = ws / "work" / slug / "build" / f"{slug}.md"
    text = task_path.read_text()
    task_path.write_text(text.replace("state: ready", "state: done"))
    rc = main(["route", "status", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "intake landed: 1" in out


def _write_harness(tmp_path):
    harness_dir = tmp_path / "harness"
    (harness_dir / ".venv" / "bin").mkdir(parents=True)
    (harness_dir / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (harness_dir / "shell.py").write_text(
        "import sys, json\n"
        "from pathlib import Path\n"
        "Path(__file__).with_name('recorded_argv.json').write_text(json.dumps(sys.argv))\n"
    )
    return harness_dir


def _wait_for(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return path.exists()


def _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=None):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"harness_dir: {harness_dir}\n"
        f"workspace_dir: {ws}\n"
        "cartridges_dir: /opt/cartridges\n"
        f"provider_profile: {provider_profile or '/opt/providers/acme.yaml'}\n"
    )
    return profile


def _write_provider_profile(tmp_path, tier="deep", effort="high"):
    """A provider profile in its real shape: `tiers` maps each tier the profile
    offers (up to `tier`) to a model, `effort` maps each to `effort`."""
    models = {"cheap": "haiku", "standard": "sonnet", "deep": "opus"}
    offered = ["cheap", "standard", "deep"][: ["cheap", "standard", "deep"].index(tier) + 1]
    tiers = "".join(f"  {name}: {models[name]}\n" for name in offered)
    efforts = "".join(f"  {name}: {effort}\n" for name in offered)
    provider = tmp_path / "provider.yaml"
    provider.write_text(f"command: claude\ntiers:\n{tiers}effort:\n{efforts}")
    return provider


def _init_repo(repo):
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def test_launch_epic_starts_the_harness_detached_with_the_recorded_argv(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "epic",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--repo", str(repo),
        "--fix-attempts", "3",
    ])
    assert rc == 0
    pid_path = ws / "runs" / "demo-1.pid"
    log_path = ws / "runs" / "demo-1.log"
    assert _wait_for(pid_path)
    assert pid_path.read_text().strip().isdigit()
    assert log_path.exists()
    recorded = harness_dir / "recorded_argv.json"
    assert _wait_for(recorded)
    argv = json.loads(recorded.read_text())
    expected = route.harness_argv(
        route.parse_profile(profile.read_text()), "epic", "demo-1",
        initiative=str(initiative_dir), repo=str(repo), fix_attempts=3,
    )
    assert argv == expected[1:]
    assert "--fix-attempts" in argv and "3" in argv


def test_launch_epic_merges_same_phase_ready_tickets_before_dispatch(tmp_path, capsys):
    # same-phase-tickets-that-share-a-file-are-merged-wire-merge-into-lint-and-launch:
    # the merge (and its file writes) must land before the detached harness
    # is started, so the harness — and anything reading the initiative dir
    # after this call — finds the merged ticket, not the original two.
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    build = initiative_dir / "p1"
    build.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    t1 = build / "t1.md"
    t2 = build / "t2.md"
    t1.write_text(
        "---\nid: t1\ntitle: First\nphase: p1\nstate: ready\nsurfaces: [agent_tools/x.py]\n---\nfirst ticket\n",
    )
    t2.write_text(
        "---\nid: t2\ntitle: Second\nphase: p1\nstate: ready\nsurfaces: [agent_tools/x.py]\n---\nsecond ticket\n",
    )
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "epic",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--repo", str(repo),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "merged: t1 + t2 -> t1 (shared agent_tools/x.py)" in out.splitlines()
    pid_path = ws / "runs" / "demo-1.pid"
    assert _wait_for(pid_path)
    merged_fields, _ = route.parse_frontmatter(t1.read_text(encoding="utf-8"))
    assert merged_fields["state"] == "ready"
    assert merged_fields["title"] == "First; Second"
    dropped_fields, _ = route.parse_frontmatter(t2.read_text(encoding="utf-8"))
    assert dropped_fields["state"] == "dropped"
    assert dropped_fields["merged_into"] == "t1"


def test_launch_threads_the_profiles_spend_window_ceiling_into_the_usage_gate(tmp_path, monkeypatch, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    profile.write_text(profile.read_text() + "spend:\n  window_ceiling_usd: 42\n")
    captured = {}

    def fake_gather(runs_dir, now, ceiling_usd=None, usage=None):
        captured["ceiling_usd"] = ceiling_usd
        return _unmeasured_window(runs_dir, now)

    monkeypatch.setattr(usage_window, "gather", fake_gather)

    rc = main([
        "route", "launch", "epic",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--repo", str(repo),
    ])
    assert rc == 0
    assert captured["ceiling_usd"] == 42.0


def test_launch_refuses_on_a_weekly_breach_even_though_the_window_is_not_close(tmp_path, monkeypatch, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    profile.write_text(profile.read_text() + "spend:\n  window_ceiling_usd: 100\n  weekly_ceiling_usd: 100\n")

    def fake_gather(runs_dir, now, ceiling_usd=None, usage=None):
        return Window(start=_USAGE_START, end=_USAGE_END, spent_usd=10.0, ceiling_usd=ceiling_usd,
                      burn_usd_per_hour=0.0, runs_in_flight=0)

    def fake_gather_weekly(runs_dir, now, weekly_ceiling_usd=None, usage=None, store_spend=None, reset=None):
        return Window(start=_USAGE_START, end=_USAGE_END, spent_usd=95.0, ceiling_usd=weekly_ceiling_usd,
                      burn_usd_per_hour=0.0, runs_in_flight=0)

    monkeypatch.setattr(usage_window, "gather", fake_gather)
    monkeypatch.setattr(usage_window, "gather_weekly", fake_gather_weekly)

    rc = main([
        "route", "launch", "epic",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--repo", str(repo),
    ])
    out = capsys.readouterr().out
    assert rc == 2
    assert "weekly" in out


def test_launch_epic_passes_node_cap_usd_from_the_profile_into_the_recorded_argv(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    profile.write_text(profile.read_text() + "spend:\n  node_cap_usd: 2.00\n")

    rc = main([
        "route", "launch", "epic",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--repo", str(repo),
    ])
    assert rc == 0
    recorded = harness_dir / "recorded_argv.json"
    assert _wait_for(recorded)
    argv = json.loads(recorded.read_text())
    assert argv[-2:] == ["--node-cap-usd", "2.0"]


def test_launch_decompose_starts_the_harness_detached_with_the_recorded_argv(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "2026-09-04-fix-thing.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    assert rc == 0
    pid_path = ws / "runs" / "fix-thing-1.pid"
    assert _wait_for(pid_path)
    recorded = harness_dir / "recorded_argv.json"
    assert _wait_for(recorded)
    argv = json.loads(recorded.read_text())
    expected = route.harness_argv(
        route.parse_profile(profile.read_text()), "decompose", "fix-thing-1",
        idea=str(idea), initiative_id="fix-thing",
    )
    assert argv == expected[1:]


def test_launch_decompose_writes_initiative_md_from_the_idea_before_launching(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "2026-09-04-fix-thing.md"
    idea.write_text(
        "---\nid: fix-thing\ntitle: Fix thing\nrepo: git@example.com:acme/widget.git\n---\n\nDo the thing.\n"
    )
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    assert rc == 0
    initiative_md = ws / "work" / "fix-thing" / "initiative.md"
    fields, body = route.parse_frontmatter(initiative_md.read_text())
    assert fields == {
        "id": "fix-thing",
        "title": "Fix thing",
        "repo": "git@example.com:acme/widget.git",
        "intake": "intake/2026-09-04-fix-thing.md",
    }
    assert body == "Do the thing."


def test_launch_decompose_leaves_an_existing_initiative_md_untouched(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    initiative_md = ws / "work" / "fix-thing" / "initiative.md"
    initiative_md.parent.mkdir(parents=True)
    initiative_md.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert initiative_md.read_text() == "---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n"
    assert sum(1 for line in out.splitlines() if "already exists" in line) == 1


def test_launch_decompose_refreshes_initiative_md_when_the_intake_changes(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\nrepo: git@example.com:acme/widget.git\n---\n\nDo the new thing.\n")
    initiative_md = ws / "work" / "fix-thing" / "initiative.md"
    initiative_md.parent.mkdir(parents=True)
    initiative_md.write_text(
        "---\nid: fix-thing\ntitle: Fix thing\nrepo: git@example.com:acme/widget.git\nintake: intake/idea.md\n---\n\nOld body.\n"
    )
    old = time.time() - 10
    os.utime(initiative_md, (old, old))
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    fields, body = route.parse_frontmatter(initiative_md.read_text())
    assert fields == {
        "id": "fix-thing",
        "title": "Fix thing",
        "repo": "git@example.com:acme/widget.git",
        "intake": "intake/idea.md",
    }
    assert body == "Do the new thing."
    assert f"routing: initiative.md refreshed from {idea}" in out


def test_launch_decompose_keeps_initiative_md_when_tickets_exist(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nDo the new thing.\n")
    initiative_md = ws / "work" / "fix-thing" / "initiative.md"
    initiative_md.parent.mkdir(parents=True)
    initiative_md.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nOld body.\n")
    ticket = ws / "work" / "fix-thing" / "build" / "fix-thing.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nid: fix-thing\n---\n\nDo it.\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert initiative_md.read_text() == "---\nid: fix-thing\ntitle: Fix thing\n---\n\nOld body.\n"
    assert "routing: initiative.md kept: tickets exist" in out


def test_launch_writes_launched_json_naming_the_lock_holder(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    fresh = datetime.datetime.now(datetime.UTC).isoformat()
    leader.write(ws / "runs", {"session": "alice", "pid": 1, "host": "some-other-host", "taken_at": fresh, "heartbeat_at": fresh})

    rc = main([
        "route", "launch", "decompose", "--label", "alice",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    assert rc == 0
    launched_path = ws / "runs" / "fix-thing-1.launched.json"
    assert _wait_for(launched_path)
    assert json.loads(launched_path.read_text())["launched_by"] == "alice"


def test_launch_does_not_reuse_a_run_id_present_only_in_the_store(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    conn = sqlite3.connect(ws / "runs" / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO runs (run_id) VALUES ('fix-thing-1')")
    conn.commit()
    conn.close()

    rc = main([
        "route", "launch", "decompose", "--no-claim",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    assert rc == 0
    assert _wait_for(ws / "runs" / "fix-thing-2.launched.json")
    assert not (ws / "runs" / "fix-thing-1.launched.json").exists()


def test_launch_with_no_claim_writes_launched_json_with_no_launched_by_key(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--no-claim",
    ])
    assert rc == 0
    launched_path = ws / "runs" / "fix-thing-1.launched.json"
    assert _wait_for(launched_path)
    assert "launched_by" not in json.loads(launched_path.read_text())


def test_launch_dry_run_prints_argv_and_starts_nothing(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose", "--dry-run",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix-thing-1" in out
    assert f"trace {ws / 'runs' / 'fix-thing-1-trace'}" in out
    assert not (ws / "runs" / "fix-thing-1.pid").exists()
    assert not (ws / "runs" / "fix-thing-1.log").exists()
    assert not (harness_dir / "recorded_argv.json").exists()


def test_launch_decompose_dry_run_prints_the_initiative_path_and_writes_nothing(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("---\nid: fix-thing\ntitle: Fix thing\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "decompose", "--dry-run",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    out = capsys.readouterr().out
    initiative_md = ws / "work" / "fix-thing" / "initiative.md"
    assert rc == 0
    assert str(initiative_md) in out
    assert not initiative_md.exists()
    assert not (harness_dir / "recorded_argv.json").exists()


def test_launch_rescue_dry_run_prints_argv_pid_log_and_trace(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    repo = tmp_path / "repo"
    _init_repo(repo)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main([
        "route", "launch", "rescue", "--dry-run",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--task", "T1",
        "--repo", str(repo),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rescue --team acme" in out
    assert f"--initiative {initiative_dir} --repo {repo} --task T1" in out
    assert f"pid {ws / 'runs' / 'demo-1.pid'}" in out
    assert f"log {ws / 'runs' / 'demo-1.log'}" in out
    assert f"trace {ws / 'runs' / 'demo-1-trace'}" in out
    assert not (ws / "runs" / "demo-1.pid").exists()


def test_launch_rescue_without_task_exits_2():
    with pytest.raises(SystemExit) as exc:
        main(["route", "launch", "rescue", "--initiative", "work/demo"])
    assert exc.value.code == 2


def test_launch_cos_dry_run_prints_argv_pid_log_and_trace(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main(["route", "launch", "cos", "--dry-run", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run:" in out and "cos" in out
    assert "pid " in out
    assert "log " in out
    assert "trace " in out
    assert not (ws / "runs" / "cos-1.pid").exists()


def test_launch_sweep_dry_run_prints_the_built_argv_with_a_label(capsys):
    rc = main([
        "route", "launch", "sweep", "--dry-run",
        "--idea", "plans/idea.md",
        "--initiative-id", "fix-thing",
        "--label", "alice",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "dry-run: sweep --idea plans/idea.md --initiative-id fix-thing --label alice\n"


def test_launch_sweep_dry_run_omits_label_when_not_passed(capsys):
    rc = main([
        "route", "launch", "sweep", "--dry-run",
        "--idea", "plans/idea.md",
        "--initiative-id", "fix-thing",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "dry-run: sweep --idea plans/idea.md --initiative-id fix-thing\n"


def test_launch_refuses_a_missing_profile(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "launch", "epic", "--profile", str(missing), "--initiative", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no profile" in out


def test_launch_cos_refuses_a_missing_profile(tmp_path, capsys):
    missing = tmp_path / "no-such-profile.yaml"
    rc = main(["route", "launch", "cos", "--profile", str(missing)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no profile" in out


def test_launch_cos_refuses_when_a_cos_run_is_already_live(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / "cos-1.pid").write_text(str(os.getpid()))  # alive: the test process itself
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "cos", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already running" in out


def test_launch_cos_refuses_while_a_held_lease_names_another_run_of_the_prefix(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / "cos-4.pid").write_text("999999999")  # dead pid: the lease alone says it is running
    leases_table(ws / "runs", ("runs:cos", "cos-4", "2999-01-01T00:00:00Z"))
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "cos", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "cos-4 is already running" in out


def test_launch_refuses_a_bad_graph_name(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    with pytest.raises(SystemExit) as exc:
        main(["route", "launch", "bogus", "--profile", str(profile)])
    assert exc.value.code == 2
    assert list((ws / "runs").iterdir()) == []


def test_launch_refuses_when_the_harness_venv_is_missing(tmp_path, capsys):
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    harness_dir = tmp_path / "harness"  # no .venv/bin/python, no shell.py
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "epic", "--profile", str(profile), "--initiative", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "harness venv missing" in out


def test_launch_epic_refuses_when_initiative_md_is_missing(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "epic", "--profile", str(profile), "--initiative", str(initiative_dir)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no initiative.md" in out


def test_launch_epic_refuses_when_the_repo_is_unresolvable(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "epic", "--profile", str(profile), "--initiative", str(initiative_dir)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no --repo" in out


def test_launch_epic_refuses_when_the_repo_is_dirty(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "dirty.txt").write_text("uncommitted\n")
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "epic", "--profile", str(profile), "--initiative", str(initiative_dir), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "uncommitted changes" in out


def test_launch_epic_refuses_when_the_initiative_already_has_a_live_run(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    (ws / "runs" / "demo-1.pid").write_text(str(os.getpid()))  # alive: the test process itself
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "epic", "--profile", str(profile), "--initiative", str(initiative_dir), "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "already running" in out


def test_launch_decompose_refuses_when_the_idea_file_is_missing(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    rc = main(["route", "launch", "decompose", "--profile", str(profile), "--idea", str(tmp_path / "no.md"), "--initiative-id", "x"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "no idea file" in out


def test_launch_refuses_a_profile_missing_a_key_harness_argv_needs(tmp_path, capsys):
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")  # no harness_dir, cartridges_dir, provider_profile
    rc = main(["route", "launch", "epic", "--profile", str(profile), "--initiative", str(tmp_path / "x")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "harness_dir not set" in out


def test_launch_refuses_a_tier_ceiling_above_the_profiles_own_ceiling(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    provider = _write_provider_profile(tmp_path, tier="standard", effort="high")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--tier-ceiling", "deep",
    ])
    out = capsys.readouterr().out
    assert rc == 2
    assert "standard" in out and "deep" in out
    assert not (ws / "runs" / "fix-thing-1.pid").exists()
    assert not list((ws / "runs").glob("*.ceiling.json"))


def test_launch_epic_with_a_tightening_ceiling_writes_the_overlaid_profile_and_ceiling_record(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    provider = _write_provider_profile(tmp_path, tier="deep", effort="high")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "epic",
        "--profile", str(profile),
        "--initiative", str(initiative_dir),
        "--repo", str(repo),
        "--tier-ceiling", "standard",
        "--effort-ceiling", "low",
    ])
    assert rc == 0
    run_id = "demo-1"
    assert _wait_for(ws / "runs" / f"{run_id}.pid")
    ceiling_path = ws / "runs" / f"{run_id}.ceiling.json"
    assert ceiling_path.exists()
    record = json.loads(ceiling_path.read_text())
    assert record["requested"] == {"tier": "standard", "effort": "low"}
    assert record["applied"] == {"tier": "standard", "effort": "low"}
    overlaid_path = Path(record["profile"])
    overlaid = yaml.safe_load(overlaid_path.read_text())
    assert overlaid["tiers"] == {"cheap": "haiku", "standard": "sonnet", "deep": "sonnet"}
    assert overlaid["effort"] == {"cheap": "low", "standard": "low", "deep": "low"}
    recorded = harness_dir / "recorded_argv.json"
    assert _wait_for(recorded)
    argv = json.loads(recorded.read_text())
    assert argv[argv.index("--provider-profile") + 1] == str(overlaid_path)


def test_launch_with_no_ceiling_flags_writes_no_ceiling_record(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    provider = _write_provider_profile(tmp_path, tier="deep", effort="high")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
    ])
    assert rc == 0
    assert _wait_for(ws / "runs" / "fix-thing-1.pid")
    assert not list((ws / "runs").glob("*.ceiling.json"))
    recorded = harness_dir / "recorded_argv.json"
    assert _wait_for(recorded)
    argv = json.loads(recorded.read_text())
    assert argv[argv.index("--provider-profile") + 1] == str(provider)


def test_launch_with_only_a_tier_ceiling_leaves_effort_unset_in_the_ceiling_record(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    provider = _write_provider_profile(tmp_path, tier="deep", effort="high")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--tier-ceiling", "standard",
    ])
    assert rc == 0
    ceiling_path = ws / "runs" / "fix-thing-1.ceiling.json"
    record = json.loads(ceiling_path.read_text())
    assert record["requested"] == {"tier": "standard", "effort": None}
    assert record["applied"] == {"tier": "standard", "effort": None}


def test_launch_dry_run_with_a_ceiling_writes_no_provider_profile_or_ceiling_file(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    provider = _write_provider_profile(tmp_path, tier="deep", effort="high")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "decompose", "--dry-run",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--tier-ceiling", "standard",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fix-thing-1.provider-profile.yaml" in out
    assert not (ws / "runs" / "fix-thing-1.provider-profile.yaml").exists()
    assert not (ws / "runs" / "fix-thing-1.ceiling.json").exists()
    assert not (ws / "runs" / "fix-thing-1.pid").exists()


def test_launch_refuses_when_the_provider_profile_is_missing_and_a_ceiling_is_requested(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)  # default provider_profile path does not exist

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--tier-ceiling", "standard",
    ])
    out = capsys.readouterr().out
    assert rc == 2
    assert "provider profile" in out
    assert not (ws / "runs" / "fix-thing-1.pid").exists()


def test_launch_refuses_when_the_provider_profile_is_not_valid_yaml(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    provider = tmp_path / "broken.yaml"
    provider.write_text("tier: [unterminated\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--tier-ceiling", "standard",
    ])
    out = capsys.readouterr().out
    assert rc == 2
    assert "not valid YAML" in out
    assert not (ws / "runs" / "fix-thing-1.pid").exists()


def _stopped_window(*_a, **_k) -> Window:
    return Window(start=_USAGE_START, end=_USAGE_END, spent_usd=95.0, ceiling_usd=10.0,
                  burn_usd_per_hour=90.0, runs_in_flight=3)


def test_launch_refuses_before_starting_when_usage_verdict_is_stop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _stopped_window)
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main(["route", "launch", "cos", "--dry-run", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage stop" in out
    assert "hard stop at 99%" in out
    assert not (ws / "runs" / "cos-1.pid").exists()


def test_launch_with_force_overrides_a_usage_stop_and_continues(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _stopped_window)
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    profile = _write_launch_profile(tmp_path, harness_dir, ws)

    rc = main(["route", "launch", "cos", "--dry-run", "--force", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "overridden by --force" in out
    assert "dry-run:" in out and "cos" in out


def _held_initiative(tmp_path):
    """An epic whose ready phase-3 task sits behind a blocked phase-2 task."""
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    initiative_dir = ws / "work" / "demo"
    (initiative_dir / "2-edge").mkdir(parents=True)
    (initiative_dir / "3-hooks").mkdir()
    (initiative_dir / "initiative.md").write_text("---\nid: demo\ntitle: Demo\n---\n\nBody\n")
    (initiative_dir / "2-edge" / "cli.md").write_text("---\nid: cli\nstate: blocked\n---\n\nHELD.\n")
    (initiative_dir / "3-hooks" / "hooks.md").write_text("---\nid: hooks\nstate: ready\n---\n\nBody\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws)
    argv = ["route", "launch", "epic", "--profile", str(profile), "--initiative", str(initiative_dir), "--repo", str(repo)]
    return ws, argv


def test_launch_epic_refuses_a_blocked_item_and_force_alone_does_not_lift_it(tmp_path, capsys):
    ws, argv = _held_initiative(tmp_path)
    rc = main([*argv, "--force"])
    out = capsys.readouterr().out
    assert rc == 2
    assert len(out.strip().splitlines()) == 1
    assert "2-edge/cli.md" in out and "--include-blocked" in out
    assert not (ws / "runs" / "demo-1.pid").exists()


def test_launch_epic_include_blocked_alone_launches_with_the_override_line(tmp_path, capsys):
    ws, argv = _held_initiative(tmp_path)
    rc = main([*argv, "--include-blocked"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "override: launching demo despite blocked 2-edge/cli.md" in out
    assert _wait_for(ws / "runs" / "demo-1.pid")


def test_launch_epic_both_flags_lift_the_blocked_guard_and_the_usage_stop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _stopped_window)
    ws, argv = _held_initiative(tmp_path)
    rc = main([*argv, "--force", "--include-blocked"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "despite blocked 2-edge/cli.md" in out
    assert "overridden by --force" in out
    assert _wait_for(ws / "runs" / "demo-1.pid")


def test_launch_epic_include_blocked_alone_does_not_lift_a_usage_stop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _stopped_window)
    ws, argv = _held_initiative(tmp_path)
    rc = main([*argv, "--include-blocked"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage stop" in out
    assert not (ws / "runs" / "demo-1.pid").exists()


def test_gather_to_assess_seam_survives_an_aware_now_with_no_stub_on_gather():
    """Regression for the naive-clock bug: `window_from` builds an aware
    window from ccusage's own ISO timestamps, and `assess` must accept an
    aware `now` without `gather` being stubbed at all."""
    blocks_json = {
        "blocks": [{
            "isActive": True,
            "startTime": "2024-01-01T00:00:00Z",
            "endTime": "2024-01-01T05:00:00Z",
            "costUSD": 5.0,
        }]
    }
    now = datetime.datetime(2024, 1, 1, 5, 0, 0, tzinfo=datetime.UTC)
    window = usage_window.window_from(blocks_json, [], now)
    result = pacing.assess(window, usage_window.DEFAULT_POLICY, now)
    assert result.verdict == "go"
    assert result.reason == "window is unmeasured: no usable ceiling_usd; reporting pace only"


def test_launch_refuses_when_the_provider_profile_does_not_parse_to_a_mapping(tmp_path, capsys):
    harness_dir = _write_harness(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir(parents=True)
    idea = ws / "intake" / "idea.md"
    idea.write_text("body\n")
    provider = tmp_path / "list.yaml"
    provider.write_text("- a\n- b\n")
    profile = _write_launch_profile(tmp_path, harness_dir, ws, provider_profile=provider)

    rc = main([
        "route", "launch", "decompose",
        "--profile", str(profile),
        "--idea", str(idea),
        "--initiative-id", "fix-thing",
        "--tier-ceiling", "standard",
    ])
    out = capsys.readouterr().out
    assert rc == 2
    assert "provider profile" in out
    assert not (ws / "runs" / "fix-thing-1.pid").exists()


def test_route_groups_prints_the_latest_dated_file(tmp_path, capsys):
    ws = tmp_path / "workspace"
    groups_dir = ws / "plans" / "intake-groups"
    groups_dir.mkdir(parents=True)
    (groups_dir / "2026-09-01.md").write_text("older\n")
    (groups_dir / "2026-09-08.md").write_text("newer\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")

    rc = main(["route", "groups", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == f"{groups_dir / '2026-09-08.md'}\n"


def test_route_groups_with_no_groups_prints_no_groups_filed(tmp_path, capsys):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")

    rc = main(["route", "groups", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "no groups filed\n"


def _pull_setup(tmp_path, monkeypatch, listing, *, extra=(), mark_argv=None):
    from types import SimpleNamespace

    from agent_tools import sources

    def list_argv(_config, repo):
        return ["echo", json.dumps([raw for raw in listing if raw["repo"] == repo] + list(extra if repo == "a/b" else ()))]

    fake = SimpleNamespace(**vars(sources.FakeAdapter(listing)), list_argv=list_argv)
    fake.mark_argv = mark_argv or fake.mark_argv
    monkeypatch.setattr(sources, "adapter_for", lambda _name: fake)
    profile, ws = _write_file_profile(tmp_path)
    with profile.open("a") as f:
        f.write('sources: {"github": {"repos": ["a/b", "x/y"]}}\nrepo_map: {"a/b": "tools"}\n')
    return profile, ws


def _issue(link, repo="a/b"):
    return {"title": "Fix it", "body": "b", "repo": repo, "link": link}


def test_pull_with_no_candidates_writes_nothing_and_says_so(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [])
    rc = main(["route", "pull", "--profile", str(profile)])
    assert rc == 0
    assert capsys.readouterr().out == "routing: pull wrote nothing: no eligible candidates\n"
    assert not (ws / "intake").exists()


def test_a_second_pull_over_the_same_link_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1")])
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    assert 'link: "https://x/1"' in written.read_text(encoding="utf-8")
    capsys.readouterr()
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    assert capsys.readouterr().out == "routing: pull wrote nothing: no eligible candidates\n"
    assert list((ws / "intake").glob("*.md")) == [written]


def test_pull_with_ids_sequence_writes_the_allocated_id(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1")])
    with profile.open("a") as f:
        f.write("ids: sequence\n")
    monkeypatch.setattr(route, "allocate_intake_id", lambda: "I7")
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    assert [p.name for p in (ws / "intake").glob("*.md")] == ["I7.md"]


def test_pull_refuses_an_unmapped_repo_by_name_and_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/2", repo="x/y")])
    rc = main(["route", "pull", "--profile", str(profile), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "'Fix it' (https://x/2): repo x/y has no mapping in the profile" in out
    assert not (ws / "intake").exists()


def test_a_pull_after_the_intake_file_is_decomposed_writes_nothing(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1")])
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    assert main(["route", "file", "--profile", str(profile), "--from-intake", str(written)]) == 0
    assert list((ws / "intake").glob("*.md")) == []
    capsys.readouterr()
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    assert capsys.readouterr().out == "routing: pull wrote nothing: no eligible candidates\n"
    assert list((ws / "intake").glob("*.md")) == []


_APPEND_ARGS = "import sys; open(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + chr(10))"


def test_pull_marks_each_written_file_and_a_failed_mark_exits_2_naming_the_link(tmp_path, monkeypatch, capsys):
    log = tmp_path / "marks.log"
    mark = lambda ref, rel: [sys.executable, "-c", _APPEND_ARGS, str(log), ref.link, rel]  # noqa: E731
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1")], mark_argv=mark)
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    assert log.read_text() == f"https://x/1 intake/{written.name}\n"

    failing = tmp_path / "failing"
    failing.mkdir()
    fail = lambda _ref, _rel: [sys.executable, "-c", "raise SystemExit(3)"]  # noqa: E731
    profile, ws = _pull_setup(failing, monkeypatch, [_issue("https://x/9")], mark_argv=fail)
    capsys.readouterr()
    assert main(["route", "pull", "--profile", str(profile)]) == 2
    assert "marking https://x/9 failed (exit 3)" in capsys.readouterr().out


def test_one_pull_files_an_intake_issue_and_reviews_then_marks_a_review_pr_under_the_pull_profile(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_tools import cli

    issue = {"title": "Fix it", "body": "b", "labels": [{"name": "intake"}], "url": "https://github.com/acme/widgets/issues/1", "repository": {"nameWithOwner": "acme/widgets"}}
    listings = {
        ("gh", "issue"): json.dumps([issue]),
        ("gh", "pr", "list"): (Path(__file__).parent / "fixtures" / "gh" / "pr-list.json").read_text(encoding="utf-8"),
    }
    ran: list[list[str]] = []

    def run(argv):
        ran.append(argv)
        return 0, next((out for prefix, out in listings.items() if tuple(argv[: len(prefix)]) == prefix), ""), ""

    monkeypatch.setattr(cli, "_run_argv", run)
    profile, ws = _write_file_profile(tmp_path)
    with profile.open("a") as f:
        f.write('sources: {"github": {"repos": ["acme/widgets"]}}\nrepo_map: {"acme/widgets": "tools"}\n')

    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    assert 'link: "https://github.com/acme/widgets/issues/1"' in written.read_text(encoding="utf-8")
    pr = "https://github.com/acme/widgets/pull/21"
    assert ran[-2:] == [
        ["cox", "runs", "review", "--pr", pr, "--profile", str(profile)],
        ["gh", "pr", "edit", "21", "--repo", "acme/widgets", "--remove-label", "review", "--add-label", "review:taken"],
    ]


def test_two_issues_on_one_path_file_the_first_and_refuse_the_second_by_link(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1"), _issue("https://x/2")])
    assert main(["route", "pull", "--profile", str(profile)]) == 2
    assert "'Fix it' (https://x/2): intake/" in capsys.readouterr().out
    [written] = (ws / "intake").glob("*.md")
    assert 'link: "https://x/1"' in written.read_text(encoding="utf-8")


def test_an_unreadable_listing_entry_is_named_and_the_rest_are_filed(tmp_path, monkeypatch, capsys):
    profile, ws = _pull_setup(tmp_path, monkeypatch, [_issue("https://x/1")], extra=[{"title": "broken"}])
    assert main(["route", "pull", "--profile", str(profile)]) == 2
    assert "skipping unreadable listing entry broken: KeyError" in capsys.readouterr().out
    assert len(list((ws / "intake").glob("*.md"))) == 1


def test_pull_unwraps_an_adapter_listing_and_hands_token_env_to_a_mark_that_asks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_tools import sources

    log = tmp_path / "marks.log"
    wrapped = lambda _config, repo: ["echo", json.dumps({"data": [_issue("https://x/1")] if repo == "a/b" else []})]  # noqa: E731
    mark = lambda ref, rel, token_env: [sys.executable, "-c", _APPEND_ARGS, str(log), ref.link, rel, token_env]  # noqa: E731
    fake = SimpleNamespace(**{
        **vars(sources.FakeAdapter([_issue("https://x/1")])),
        "list_argv": wrapped, "unwrap": lambda payload, _repo: payload["data"],
        "MARK_NEEDS_TOKEN_ENV": True, "mark_argv": mark,
    })
    monkeypatch.setattr(sources, "adapter_for", lambda _name: fake)
    profile, ws = _write_file_profile(tmp_path)
    with profile.open("a") as f:
        f.write('sources: {"github": {"repos": ["a/b"], "token_env": "ACME_TOKEN"}}\nrepo_map: {"a/b": "tools"}\n')
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    [written] = (ws / "intake").glob("*.md")
    assert log.read_text() == f"https://x/1 intake/{written.name} ACME_TOKEN\n"


def _folder_profile(tmp_path, *, github=False):
    drop = tmp_path / "drop"
    drop.mkdir()
    profile, ws = _write_file_profile(tmp_path)
    blocks = {"folder": {"repos": [str(drop)]}, **({"github": {"repos": ["a/b"]}} if github else {})}
    with profile.open("a") as f:
        f.write(f"sources: {json.dumps(blocks)}\nrepo_map: {json.dumps({str(drop): 'tools', 'a/b': 'tools'})}\n")
    return profile, ws, drop


def test_a_folder_pull_files_each_dropped_file_and_moves_it_to_done(tmp_path):
    profile, ws, drop = _folder_profile(tmp_path)
    (drop / "dark-mode.md").write_text("# Add a dark mode\nUsers want it.\n")
    assert main(["route", "pull", "--profile", str(profile), "--source", "folder"]) == 0
    [written] = (ws / "intake").glob("*.md")
    text = written.read_text(encoding="utf-8")
    assert "title: \"Add a dark mode\"" in text or "title: Add a dark mode" in text
    assert "repo: tools" in text and "source: folder" in text and "Users want it." in text
    assert not (drop / "dark-mode.md").exists()
    assert (drop / "done" / "dark-mode.md").exists()


def test_a_bare_pull_reads_every_configured_source_naming_each_that_wrote_nothing(tmp_path, monkeypatch, capsys):
    from agent_tools import cli

    profile, ws, drop = _folder_profile(tmp_path, github=True)
    (drop / "a.md").write_text("# From the folder\n")
    real_run = cli._run_argv
    monkeypatch.setattr(cli, "_run_argv", lambda argv: (0, "[]", "") if argv[0] == "gh" else real_run(argv))
    assert main(["route", "pull", "--profile", str(profile)]) == 0
    out = capsys.readouterr().out
    assert "routing: pull wrote nothing from github: no eligible candidates\n" in out
    assert "wrote nothing from folder" not in out
    assert len(list((ws / "intake").glob("*.md"))) == 1


def test_a_bare_pull_with_no_sources_says_so(tmp_path, capsys):
    profile, _ws = _write_file_profile(tmp_path)
    assert main(["route", "pull", "--profile", str(profile)]) == 2
    assert capsys.readouterr().out == "routing: no sources in the profile\n"
