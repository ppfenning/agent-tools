import contextlib
import datetime
import json
import os
import socket

from agent_tools import chair, leader_chat
from agent_tools.cli import _leader_heartbeat_minutes, _leader_identity, _leader_launched_by, main

_NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
_DEAD_PID = 999999999
_LIVE_RECORD = {"session": "alice", "pid": 4242, "host": "h1", "taken_at": _NOW.isoformat(), "heartbeat_at": _NOW.isoformat()}
_STALE_HEARTBEAT_RECORD = {**_LIVE_RECORD, "heartbeat_at": (_NOW - datetime.timedelta(minutes=99)).isoformat()}


def _profile(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n")
    return profile


def _fresh_iso():
    """Real wall-clock time, for a CLI test whose code under test calls
    `datetime.datetime.now(UTC)` itself and must see a lock as heartbeat-fresh."""
    return datetime.datetime.now(datetime.UTC).isoformat()


# -- liveness: pure, on literals --------------------------------------------------


def test_liveness_is_none_with_no_record():
    assert chair.liveness(None, False, _NOW, "h1") == "none"


def test_liveness_is_crashed_when_the_pid_is_dead_on_the_records_own_host():
    assert chair.liveness(_LIVE_RECORD, False, _NOW, "h1") == "crashed"


def test_liveness_is_live_when_the_pid_is_dead_on_another_host():
    assert chair.liveness(_LIVE_RECORD, False, _NOW, "h2") == "live"


def test_liveness_is_live_when_the_pid_is_alive_and_the_heartbeat_is_fresh():
    assert chair.liveness(_LIVE_RECORD, True, _NOW, "h1") == "live"


def test_liveness_is_stale_when_the_heartbeat_outages_the_policy_minutes_regardless_of_pid():
    assert chair.liveness(_STALE_HEARTBEAT_RECORD, False, _NOW, "h1", heartbeat_minutes=10) == "stale"


def test_liveness_is_stale_not_raised_for_an_unparsable_heartbeat_at():
    assert chair.liveness({**_LIVE_RECORD, "heartbeat_at": "not-a-timestamp"}, True, _NOW, "h1") == "stale"


# -- take/beat/release: pure, on literals ------------------------------------------


def test_take_refuses_a_live_lock_and_names_the_holder():
    record, reason = chair.take(_LIVE_RECORD, "bob", 99, "h2", _NOW, 10, True)
    assert record is None
    assert "alice" in reason and "4242" in reason and "h1" in reason


def test_take_refuses_a_stale_lock_without_steal():
    record, reason = chair.take(_STALE_HEARTBEAT_RECORD, "bob", 99, "h2", _NOW, 10, False, steal=False)
    assert record is None
    assert "alice" in reason


def test_take_with_steal_succeeds_against_a_stale_lock():
    record, reason = chair.take(_STALE_HEARTBEAT_RECORD, "bob", 99, "h2", _NOW, 10, False, steal=True)
    assert reason == ""
    assert record == {"session": "bob", "pid": 99, "host": "h2", "taken_at": _NOW.isoformat(), "heartbeat_at": _NOW.isoformat(), "runs": [], "claude_session": None}


def test_take_refuses_a_crashed_lock_without_steal_and_names_it_crashed():
    record, reason = chair.take(_LIVE_RECORD, "bob", 99, "h1", _NOW, 10, False, steal=False)
    assert record is None
    assert "crashed" in reason


def test_beat_refreshes_the_heartbeat_and_appends_a_run_for_the_matching_triple():
    record, reason = chair.beat(_LIVE_RECORD, "alice", 4242, "h1", _NOW + datetime.timedelta(minutes=1), run_id="cos-1")
    assert reason == ""
    assert record["heartbeat_at"] == (_NOW + datetime.timedelta(minutes=1)).isoformat()
    assert record["runs"] == ["cos-1"]


def test_beat_errors_for_a_session_that_does_not_hold_it():
    record, reason = chair.beat(_LIVE_RECORD, "bob", 4242, "h1", _NOW)
    assert record is None and "not held by bob" in reason


def test_beat_errors_for_the_right_session_with_the_wrong_pid():
    """The label alone is not proof of identity: a matching session with a pid the
    lock did not record is not the process that took it."""
    record, reason = chair.beat(_LIVE_RECORD, "alice", 99999, "h1", _NOW)
    assert record is None and "not held by alice" in reason


def test_release_clears_the_lock_for_the_matching_triple():
    record, reason = chair.release(_LIVE_RECORD, "alice", 4242, "h1")
    assert record is None and reason == ""


def test_release_refuses_a_session_that_does_not_hold_it():
    record, reason = chair.release(_LIVE_RECORD, "bob", 99, "h2")
    assert record == _LIVE_RECORD and "not held by bob" in reason


def test_leader_heartbeat_minutes_is_the_documented_default():
    """Cartridge policy resolution is another repository's item (out of scope here);
    this always answers the default until that lands."""
    assert _leader_heartbeat_minutes() == chair.DEFAULT_HEARTBEAT_MINUTES == 10


# -- _leader_launched_by: pure, on literals ----------------------------------------


def test_leader_launched_by_is_the_holders_session_when_the_lock_is_live():
    assert _leader_launched_by(_LIVE_RECORD, True, _NOW, "h1", 10) == "alice"


def test_leader_launched_by_is_none_when_the_lock_is_not_live():
    assert _leader_launched_by(_STALE_HEARTBEAT_RECORD, True, _NOW, "h1", 10) is None


# -- CLI: a tmp runs dir under a tmp profile ---------------------------------------


def test_cli_status_reports_crashed_for_a_lock_whose_pid_is_dead_on_this_host(tmp_path, capsys):
    """The heartbeat is fresh by the real clock and the host matches this one, so only
    the dead pid can be making this crashed — isolating the fact the status wording
    claims to report."""
    profile = _profile(tmp_path)
    fresh = _fresh_iso()
    chair.write(tmp_path / "runs", {**_LIVE_RECORD, "host": socket.gethostname(), "pid": _DEAD_PID, "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "status", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "crashed" in out and "stale" not in out
    assert "leader" not in out


def test_cli_status_treats_a_foreign_hosts_lock_as_live_since_it_cannot_check_the_remote_pid(tmp_path, capsys):
    """A pid this process cannot check locally must not be judged dead just because
    the number happens to look unused on this machine."""
    profile = _profile(tmp_path)
    fresh = _fresh_iso()
    chair.write(tmp_path / "runs", {**_LIVE_RECORD, "host": "some-other-host", "pid": _DEAD_PID, "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "status", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "live" in out and "stale" not in out
    assert "leader" not in out


def test_cli_take_refuses_a_fresh_live_lock(tmp_path, capsys):
    profile = _profile(tmp_path)
    fresh = _fresh_iso()
    chair.write(tmp_path / "runs", {**_LIVE_RECORD, "host": socket.gethostname(), "pid": os.getpid(), "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "take", "--profile", str(profile), "--label", "bob"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "alice" in out
    assert "leader" not in out


def test_cli_take_steal_succeeds_against_a_stale_lock(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": _DEAD_PID, "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "take", "--profile", str(profile), "--label", "bob", "--steal"])
    assert rc == 0
    assert chair.read(runs_dir)["session"] == "bob"


def _stub_acquire_lease(monkeypatch):
    seen = []
    monkeypatch.setattr(chair, "acquire_lease", lambda *args, **kwargs: seen.append(kwargs) or "")
    return seen


def test_cli_take_steal_steals_the_store_lease_too(tmp_path, monkeypatch):
    seen = _stub_acquire_lease(monkeypatch)
    assert main(["route", "chair", "take", "--profile", str(_profile(tmp_path)), "--label", "bob", "--steal"]) == 0
    assert seen == [{"steal": True}]


def test_cli_take_without_steal_leaves_the_store_lease_alone(tmp_path, monkeypatch):
    seen = _stub_acquire_lease(monkeypatch)
    assert main(["route", "chair", "take", "--profile", str(_profile(tmp_path)), "--label", "bob"]) == 0
    assert seen == [{"steal": False}]


def test_cli_take_then_beat_prints_the_chair_worded_lines(tmp_path, capsys):
    profile = _profile(tmp_path)
    assert main(["route", "chair", "take", "--profile", str(profile), "--label", "cos1"]) == 0
    assert "chair taken:" in capsys.readouterr().out
    assert main(["route", "chair", "beat", "--profile", str(profile), "--label", "cos1", "--run", "run-42"]) == 0
    assert "chair heartbeat:" in capsys.readouterr().out


def test_cli_take_then_beat_succeeds_for_the_same_session(tmp_path, capsys):
    """`take` and `beat` both identify the caller by `os.getppid()` — the process that
    invoked `cox`, not the ephemeral `cox` process itself — so two separate CLI
    invocations from the same long-running session are the same holder."""
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    assert main(["route", "chair", "take", "--profile", str(profile), "--label", "cos1"]) == 0
    capsys.readouterr()
    assert main(["route", "chair", "beat", "--profile", str(profile), "--label", "cos1", "--run", "run-42"]) == 0
    assert chair.read(runs_dir)["runs"] == ["run-42"]


def test_cli_take_then_release_succeeds_for_the_same_session(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    assert main(["route", "chair", "take", "--profile", str(profile), "--label", "cos1"]) == 0
    capsys.readouterr()
    assert main(["route", "chair", "release", "--profile", str(profile), "--label", "cos1"]) == 0
    assert chair.read(runs_dir) is None


def test_identity_prefers_an_explicit_pid_over_the_parent():
    assert _leader_identity(4242)[0] == 4242
    assert _leader_identity()[0] == os.getppid()


def test_cli_take_refuses_an_explicit_dead_pid_and_writes_nothing(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    rc = main(["route", "chair", "take", "--profile", str(profile), "--pid", str(_DEAD_PID)])
    out = capsys.readouterr().out
    assert rc == 2
    assert str(_DEAD_PID) in out
    assert chair.read(runs_dir) is None


def test_cli_take_with_an_explicit_live_pid_still_takes(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    rc = main(["route", "chair", "take", "--profile", str(profile), "--pid", str(os.getpid())])
    assert rc == 0
    assert chair.read(runs_dir)["pid"] == os.getpid()


def test_cli_clear_removes_a_dead_pid_lock_and_names_what_it_removed(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": _DEAD_PID, "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "clear", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alice" in out
    assert "leader" not in out
    assert chair.read(runs_dir) is None


def test_cli_clear_refuses_a_live_lock_without_force(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": os.getpid(), "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "clear", "--profile", str(profile)])
    assert rc == 0
    assert chair.read(runs_dir) is not None


def test_cli_clear_with_force_removes_a_live_lock(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": os.getpid(), "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "clear", "--profile", str(profile), "--force"])
    assert rc == 0
    assert chair.read(runs_dir) is None


def test_cli_clear_holds_the_chair_lock_across_the_read_decide_unlink_sequence(tmp_path, capsys, monkeypatch):
    """A `take` landing between clear's read and its unlink must not be discarded
    silently, so `_route_chair_clear` must hold `chair.locked` while `chair.clear` runs."""
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": _DEAD_PID, "taken_at": fresh, "heartbeat_at": fresh})

    held = []
    real_locked = chair.locked

    @contextlib.contextmanager
    def spying_locked(runs_dir_):
        with real_locked(runs_dir_):
            held.append(True)
            try:
                yield
            finally:
                held.append(False)

    real_clear = chair.clear

    def spying_clear(runs_dir_, force=False):
        assert held and held[-1] is True, "chair.clear ran without chair.locked held"
        return real_clear(runs_dir_, force=force)

    monkeypatch.setattr(chair, "locked", spying_locked)
    monkeypatch.setattr(chair, "clear", spying_clear)
    rc = main(["route", "chair", "clear", "--profile", str(profile)])
    assert rc == 0
    assert held == [True, False]


# -- chat: operator send/read, and --as-leader identity ---------------------------


def test_cli_chat_operator_send_then_read_shows_the_thread_as_json(tmp_path, capsys):
    profile = _profile(tmp_path)
    assert main(["route", "chair", "chat", "hello", "--profile", str(profile)]) == 0
    capsys.readouterr()
    rc = main(["route", "chair", "chat", "--read", "--json", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    entries = json.loads(out)
    assert entries == [{"at": entries[0]["at"], "from": "operator", "text": "hello"}]


def test_cli_chat_as_leader_is_refused_when_the_lock_is_held_by_someone_else(tmp_path, capsys):
    """A different, live pid on this host names a real other holder, not a crashed one."""
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": os.getpid(), "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "chat", "reply", "--as-leader", "--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "alice" in out
    assert leader_chat.chat_path(runs_dir).exists() is False


def test_cli_chat_as_leader_succeeds_when_the_caller_is_the_live_holder(tmp_path, capsys):
    profile = _profile(tmp_path)
    runs_dir = tmp_path / "runs"
    fresh = _fresh_iso()
    chair.write(runs_dir, {**_LIVE_RECORD, "host": socket.gethostname(), "pid": os.getppid(), "taken_at": fresh, "heartbeat_at": fresh})
    rc = main(["route", "chair", "chat", "reply", "--as-leader", "--profile", str(profile)])
    assert rc == 0
    thread = leader_chat.read_thread(leader_chat.chat_path(runs_dir).read_text(encoding="utf-8"), limit=10)
    assert thread == [{"at": thread[0]["at"], "from": "alice", "text": "reply"}]
