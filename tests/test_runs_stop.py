import json
import signal
from pathlib import Path

from agent_tools.lane_hosts import LaneHost
from agent_tools.runs_stop import stop_kill_argv, stop_run


def test_stop_kill_argv_literal():
    assert stop_kill_argv("jarvis.tail", "/home/x/ws/runs/r-1.pid") == [
        "ssh",
        "-o", "ConnectTimeout=30",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "jarvis.tail",
        "sh -c 'kill -TERM $(cat /home/x/ws/runs/r-1.pid)'",
    ]


def _sequence(*values):
    calls = []

    def fake(*args):
        calls.append(args)
        index = len(calls) - 1
        return values[index] if index < len(values) else values[-1]

    fake.calls = calls
    return fake


def test_stop_run_local_success(tmp_path: Path):
    run_id = "r-local-1"
    (tmp_path / f"{run_id}.pid").write_text("4242")

    kill_local = _sequence(None)
    is_ended = _sequence(False, True)
    sleep = _sequence(None)

    result = stop_run(
        run_id,
        tmp_path,
        kill_local=kill_local,
        run_remote=_sequence(None),
        is_ended=is_ended,
        sleep=sleep,
    )

    assert result == {"ok": True, "run": run_id, "host": "local"}
    assert kill_local.calls == [(4242, signal.SIGTERM)]
    assert len(sleep.calls) == 1


def test_stop_run_local_no_pidfile(tmp_path: Path):
    run_id = "r-local-missing"
    kill_local = _sequence(None)

    result = stop_run(
        run_id,
        tmp_path,
        kill_local=kill_local,
        run_remote=_sequence(None),
        is_ended=_sequence(True),
        sleep=_sequence(None),
    )

    assert result == {"ok": False, "run": run_id, "reason": "no pidfile"}
    assert kill_local.calls == []


def test_stop_run_remote_success(tmp_path: Path):
    run_id = "r-remote-1"
    (tmp_path / f"{run_id}.remote.json").write_text(
        json.dumps({"host": "jarvis", "launched_at": "2026-09-29T00:00:00Z"})
    )
    hosts = (LaneHost("jarvis", "jarvis.tail", "/home/x/ws"),)
    run_remote = _sequence(True)

    result = stop_run(
        run_id,
        tmp_path,
        kill_local=_sequence(None),
        run_remote=run_remote,
        is_ended=_sequence(True),
        sleep=_sequence(None),
        hosts=hosts,
    )

    assert result == {"ok": True, "run": run_id, "host": "jarvis"}
    assert run_remote.calls == [
        (stop_kill_argv("jarvis.tail", f"/home/x/ws/runs/{run_id}.pid"),)
    ]


def test_stop_run_remote_host_not_configured(tmp_path: Path):
    run_id = "r-remote-2"
    (tmp_path / f"{run_id}.remote.json").write_text(
        json.dumps({"host": "jarvis", "launched_at": "2026-09-29T00:00:00Z"})
    )
    run_remote = _sequence(True)

    result = stop_run(
        run_id,
        tmp_path,
        kill_local=_sequence(None),
        run_remote=run_remote,
        is_ended=_sequence(True),
        sleep=_sequence(None),
        hosts=(),
    )

    assert result == {"ok": False, "run": run_id, "reason": "host jarvis not configured"}
    assert run_remote.calls == []


def test_stop_run_timeout(tmp_path: Path):
    run_id = "r-timeout"
    (tmp_path / f"{run_id}.pid").write_text("777")

    sleep = _sequence(None, None)

    result = stop_run(
        run_id,
        tmp_path,
        kill_local=_sequence(None),
        run_remote=_sequence(None),
        is_ended=_sequence(False, False, False),
        sleep=sleep,
        max_seconds=4,
        interval=2,
    )

    assert len(sleep.calls) == 2
    assert result == {"ok": False, "run": run_id, "reason": "timed out waiting for exit"}
