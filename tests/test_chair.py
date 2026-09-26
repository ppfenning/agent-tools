"""beat_loop: a fake clock and a fake pid-liveness check drive its while-alive
loop with no real wait and no real process ever needing to die."""

from __future__ import annotations

import datetime
import json
import os
import socket
import subprocess
import types

import pytest

from agent_tools import chair, store_cli
from agent_tools.cli import main


class _FakeClock:
    def __init__(self) -> None:
        self.ticks = 0

    def sleep(self, seconds: float) -> None:
        self.ticks += 1


def test_two_iterations_then_the_pid_dies_beats_twice_and_returns(tmp_path, monkeypatch):
    calls = []

    def fake_beat(*args, **kwargs):
        calls.append(args)
        return {"heartbeat_at": "x"}, ""

    monkeypatch.setattr(chair, "beat", fake_beat)
    outcomes = iter([True, True, False])

    def fake_alive(pid: int, sig: int) -> None:
        if not next(outcomes):
            raise ProcessLookupError()

    clock = _FakeClock()
    rc = chair.beat_loop("chair-test", 4321, runs_dir=tmp_path, interval=0, clock=clock, alive=fake_alive)

    assert rc == 0
    assert len(calls) == 2
    assert clock.ticks == 2


def test_one_tick_against_a_real_record_renews_heartbeat_at_on_disk(tmp_path):
    session, pid, host = "chair-test", 4321, socket.gethostname()
    taken_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC).isoformat()
    chair.write(tmp_path, {"session": session, "pid": pid, "host": host, "taken_at": taken_at, "heartbeat_at": taken_at, "runs": []})
    outcomes = iter([True, False])

    def fake_alive(pid: int, sig: int) -> None:
        if not next(outcomes):
            raise ProcessLookupError()

    rc = chair.beat_loop(session, pid, runs_dir=tmp_path, interval=0, clock=_FakeClock(), alive=fake_alive)

    assert rc == 0
    assert chair.read(tmp_path)["heartbeat_at"] != taken_at


def test_a_refusal_from_beat_is_printed_and_stops_the_loop_at_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chair, "beat", lambda *a, **k: (None, "chair: not held"))

    rc = chair.beat_loop("chair-test", 4321, runs_dir=tmp_path, interval=0, clock=_FakeClock(), alive=lambda pid, sig: None)

    assert rc == 2
    assert "chair: not held" in capsys.readouterr().out


def test_a_permission_error_from_alive_is_treated_as_still_alive_not_gone(tmp_path, monkeypatch):
    calls = []

    def fake_beat(*args, **kwargs):
        calls.append(args)
        return {"heartbeat_at": "x"}, ""

    monkeypatch.setattr(chair, "beat", fake_beat)
    outcomes = iter([PermissionError(), True, False])

    def fake_alive(pid: int, sig: int) -> None:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if not outcome:
            raise ProcessLookupError()

    rc = chair.beat_loop("chair-test", 4321, runs_dir=tmp_path, interval=0, clock=_FakeClock(), alive=fake_alive)

    assert rc == 0
    assert len(calls) == 2


_NOW = datetime.datetime(2026, 9, 24, 14, 30, tzinfo=datetime.UTC)


def test_take_records_the_claude_session_or_null():
    with_id, _ = chair.take(None, "c", 1, "h", _NOW, 10, False, claude_session="abc-123")
    without, _ = chair.take(None, "c", 1, "h", _NOW, 10, False)
    assert with_id["claude_session"] == "abc-123"
    assert without["claude_session"] is None


def test_beat_records_keeps_and_backfills_the_claude_session():
    taken, _ = chair.take(None, "c", 1, "h", _NOW, 10, False, claude_session="abc-123")
    legacy = {k: v for k, v in taken.items() if k != "claude_session"}
    assert chair.beat(taken, "c", 1, "h", _NOW, claude_session="new")[0]["claude_session"] == "new"
    assert chair.beat(taken, "c", 1, "h", _NOW)[0]["claude_session"] == "abc-123"
    assert chair.beat(legacy, "c", 1, "h", _NOW)[0]["claude_session"] is None


def test_claude_session_from_env_reads_the_variable_and_never_fails():
    assert chair.claude_session_from_env({"CLAUDE_SESSION_ID": "abc"}) == "abc"
    assert chair.claude_session_from_env({}) is None
    assert chair.claude_session_from_env({"CLAUDE_SESSION_ID": ""}) is None


def test_claude_session_from_env_prefers_claude_code_session_id():
    assert chair.claude_session_from_env({"CLAUDE_CODE_SESSION_ID": "s1"}) == "s1"
    assert chair.claude_session_from_env({"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_SESSION_ID": "old"}) == "s1"
    assert chair.claude_session_from_env({"CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_ID": "old"}) == "old"


def test_beat_loop_writes_the_claude_session_from_environ_to_disk(tmp_path):
    session, pid, host = "chair-test", 4321, socket.gethostname()
    record, _ = chair.take(None, session, pid, host, _NOW, 10, False)
    chair.write(tmp_path, record)
    outcomes = iter([True, False])

    def fake_alive(pid: int, sig: int) -> None:
        if not next(outcomes):
            raise ProcessLookupError()

    env = {"CLAUDE_SESSION_ID": "abc-123"}
    chair.beat_loop(session, pid, runs_dir=tmp_path, interval=0, clock=_FakeClock(), alive=fake_alive, environ=env)

    assert chair.read(tmp_path)["claude_session"] == "abc-123"


_HOLDER = "chair-test@h:1"


def _harness(monkeypatch, code: int, stdout: str) -> list[list[str]]:
    """A present harness whose every store_cli call exits `code` printing `stdout`; returns the argv list it records."""
    argvs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argvs.append(argv)
        return types.SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    monkeypatch.setattr(store_cli, "_harness_python", lambda: "/h/python")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return argvs


def test_lease_holder_joins_session_host_and_pid():
    assert chair.lease_holder("chair-test", 1, "h") == _HOLDER


def test_lease_verdict_maps_each_result_to_an_action():
    assert chair.lease_verdict(store_cli.LeaseGranted(4, _HOLDER)) == ("granted", "")
    assert chair.lease_verdict(store_cli.LeaseRefused(4, "other@h:9")) == ("refused", "chair: held by other@h:9 (store lease)")
    assert chair.lease_verdict(store_cli.LeaseRefused(None, None)) == ("refused", "chair: the store lease was refused")
    assert chair.lease_verdict(store_cli.LeaseError("exit 2: bad")) == ("fallback", "chair: warning: store lease failed (exit 2: bad); using the lock file only")
    assert chair.lease_verdict(store_cli.NotAvailable()) == ("fallback", "chair: warning: no harness found; using the lock file only")


def test_claim_with_no_lease_held_acquires_and_stores_the_epoch(tmp_path, monkeypatch):
    argvs = _harness(monkeypatch, 0, json.dumps({"ok": True, "epoch": 7, "holder": _HOLDER}))

    assert chair.acquire_lease(tmp_path, "chair-test", 1, "h") == ""

    assert argvs[0][-8:-2] == ["lease", "acquire", "chair", _HOLDER, "--ttl", "600"]
    assert argvs[0][-2] == "--store-url"
    assert json.loads((tmp_path / chair.LEASE_FILENAME).read_text()) == {"holder": _HOLDER, "epoch": 7}


def test_claim_refused_by_a_foreign_live_lease_returns_the_refusal_and_stores_nothing(tmp_path, monkeypatch):
    _harness(monkeypatch, 3, json.dumps({"ok": False, "epoch": 7, "holder": "other@h:9"}))

    assert chair.acquire_lease(tmp_path, "chair-test", 1, "h") == "chair: held by other@h:9 (store lease)"
    assert not (tmp_path / chair.LEASE_FILENAME).exists()


def test_claim_with_an_exit_2_falls_back_with_a_warning(tmp_path, monkeypatch, capsys):
    _harness(monkeypatch, 2, json.dumps({"error": "unreadable store"}))

    assert chair.acquire_lease(tmp_path, "chair-test", 1, "h") == ""

    assert "warning: store lease failed" in capsys.readouterr().err
    assert not (tmp_path / chair.LEASE_FILENAME).exists()


def test_claim_with_no_harness_falls_back_with_a_warning_and_spawns_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(store_cli, "_harness_python", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no harness, so nothing may spawn"))

    assert chair.acquire_lease(tmp_path, "chair-test", 1, "h") == ""

    assert "no harness found" in capsys.readouterr().err


def test_claim_whose_spawn_fails_falls_back_with_a_warning(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(store_cli, "_harness_python", lambda: "/h/python")
    monkeypatch.setattr(subprocess, "run", boom)

    assert chair.acquire_lease(tmp_path, "chair-test", 1, "h") == ""
    assert "exec failed" in capsys.readouterr().err


def test_beat_renews_the_stored_lease_with_its_epoch(tmp_path, monkeypatch):
    (tmp_path / chair.LEASE_FILENAME).write_text(json.dumps({"holder": _HOLDER, "epoch": 7}))
    argvs = _harness(monkeypatch, 0, json.dumps({"ok": True, "epoch": 8, "holder": _HOLDER}))

    assert chair.renew_lease(tmp_path, "chair-test", 1, "h") == ""

    assert argvs[0][-9:-2] == ["lease", "renew", "chair", _HOLDER, "7", "--ttl", "600"]
    assert json.loads((tmp_path / chair.LEASE_FILENAME).read_text())["epoch"] == 8


def test_beat_with_a_lost_lease_returns_the_refusal(tmp_path, monkeypatch):
    (tmp_path / chair.LEASE_FILENAME).write_text(json.dumps({"holder": _HOLDER, "epoch": 7}))
    _harness(monkeypatch, 3, json.dumps({"ok": False, "epoch": 9, "holder": "other@h:9"}))

    assert chair.renew_lease(tmp_path, "chair-test", 1, "h") == "chair: held by other@h:9 (store lease)"


def test_beat_without_a_stored_lease_acquires_it(tmp_path, monkeypatch):
    """A chair taken before the store lease existed picks the lease up on its next beat."""
    argvs = _harness(monkeypatch, 0, json.dumps({"ok": True, "epoch": 1, "holder": _HOLDER}))

    assert chair.renew_lease(tmp_path, "chair-test", 1, "h") == ""

    assert argvs[0][-8:-2] == ["lease", "acquire", "chair", _HOLDER, "--ttl", "600"]
    assert json.loads((tmp_path / chair.LEASE_FILENAME).read_text())["epoch"] == 1


def test_beat_loop_stops_at_once_when_the_lease_is_lost(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chair, "beat", lambda *a, **k: ({"heartbeat_at": "x"}, ""))
    monkeypatch.setattr(chair, "renew_lease", lambda *a, **k: "chair: held by other@h:9 (store lease)")

    rc = chair.beat_loop("chair-test", 4321, runs_dir=tmp_path, interval=0, clock=_FakeClock(), alive=lambda pid, sig: None)

    assert rc == 2
    assert "held by other@h:9" in capsys.readouterr().out
    assert chair.read(tmp_path) is None


def test_release_releases_the_stored_lease_and_removes_the_sidecar(tmp_path, monkeypatch):
    (tmp_path / chair.LEASE_FILENAME).write_text(json.dumps({"holder": _HOLDER, "epoch": 7}))
    argvs = _harness(monkeypatch, 0, json.dumps({"ok": True, "epoch": 7, "holder": _HOLDER}))

    chair.release_lease(tmp_path, "chair-test", 1, "h")

    assert argvs[0][-7:-2] == ["lease", "release", "chair", _HOLDER, "7"]
    assert not (tmp_path / chair.LEASE_FILENAME).exists()


def test_release_of_a_released_reply_prints_nothing_on_stderr(tmp_path, monkeypatch, capsys):
    (tmp_path / chair.LEASE_FILENAME).write_text(json.dumps({"holder": _HOLDER, "epoch": 7}))
    _harness(monkeypatch, 0, json.dumps({"ok": True, "epoch": None, "holder": None}))

    chair.release_lease(tmp_path, "chair-test", 1, "h")

    assert capsys.readouterr().err == ""
    assert not (tmp_path / chair.LEASE_FILENAME).exists()


def test_acquire_given_a_released_reply_warns_it_was_not_granted_and_stores_nothing(tmp_path, monkeypatch, capsys):
    _harness(monkeypatch, 0, json.dumps({"ok": True, "epoch": None, "holder": None}))

    assert chair.acquire_lease(tmp_path, "chair-test", 1, "h") == ""

    assert "answered released, not granted" in capsys.readouterr().err
    assert not (tmp_path / chair.LEASE_FILENAME).exists()


def test_release_with_a_refused_lease_warns_and_still_removes_the_sidecar(tmp_path, monkeypatch, capsys):
    (tmp_path / chair.LEASE_FILENAME).write_text(json.dumps({"holder": _HOLDER, "epoch": 7}))
    _harness(monkeypatch, 3, json.dumps({"ok": False, "epoch": 9, "holder": "other@h:9"}))

    chair.release_lease(tmp_path, "chair-test", 1, "h")

    assert "held by other@h:9" in capsys.readouterr().err
    assert not (tmp_path / chair.LEASE_FILENAME).exists()


def _take_over_live_record(tmp_path, monkeypatch, record_session: str, *flags: str):
    """Run `chair take --label bob` against a fresh live record; return (rc, acquire_lease kwargs, runs_dir)."""
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n")
    runs_dir = tmp_path / "runs"
    fresh = datetime.datetime.now(datetime.UTC).isoformat()
    chair.write(runs_dir, {"session": record_session, "pid": os.getpid(), "host": socket.gethostname(), "taken_at": fresh, "heartbeat_at": fresh, "runs": ["run-1"], "claude_session": None})
    seen: list[dict] = []
    monkeypatch.setattr(chair, "acquire_lease", lambda *args, **kwargs: seen.append(kwargs) or "")
    rc = main(["route", "chair", "take", "--profile", str(profile), "--label", "bob", "--pid", str(os.getpid()), *flags])
    return rc, seen, runs_dir


def test_take_steal_on_own_live_record_retakes_the_lease(tmp_path, monkeypatch, capsys):
    rc, seen, runs_dir = _take_over_live_record(tmp_path, monkeypatch, "bob", "--steal")
    assert rc == 0
    assert capsys.readouterr().out.startswith("chair lease retaken: bob (pid ")
    assert seen == [{"steal": True}]
    assert chair.read(runs_dir)["runs"] == ["run-1"]


def test_take_without_steal_on_own_live_record_is_refused_and_leaves_the_lease(tmp_path, monkeypatch, capsys):
    rc, seen, _runs_dir = _take_over_live_record(tmp_path, monkeypatch, "bob")
    assert rc == 2
    assert capsys.readouterr().out.startswith("chair: held by bob")
    assert seen == []


def test_take_steal_on_another_sessions_live_record_is_refused(tmp_path, monkeypatch, capsys):
    rc, seen, _runs_dir = _take_over_live_record(tmp_path, monkeypatch, "alice", "--steal")
    assert rc == 2
    assert capsys.readouterr().out.startswith("chair: held by alice")
    assert seen == []
