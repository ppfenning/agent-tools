import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_pid_probe, cli, run_store
from agent_tools.chair_pid_probe import pid_check_argv, pid_probe_of, pidfile_path, read_pid_probe
from agent_tools.chair_plan import _silent_dead_pid, _with_dead_pid_lost
from agent_tools.lane_hosts import LaneHost

NOW = "2026-10-05T12:00:00Z"
FRESH = "2026-10-05T11:58:00Z"
STALE = "2026-10-05T11:30:00Z"
LANE_BEAT = "2026-10-05T11:59:00Z"


def _lane(run: str = "alpha-2", host: str = "h", beat: str = LANE_BEAT, launched: str = "2026-10-05T10:00:00Z") -> run_store.Lane:
    return run_store.Lane(run, host, launched, beat)


def _host(beat_at: str, name: str = "h") -> dict:
    return {"name": name, "beat_at": beat_at, "ssh": "me@h", "state": "active", "versions_json": '{"workspace_dir": "/ws"}'}


def test_a_dead_pid_on_a_fresh_host_is_alive_false():
    assert pid_probe_of([_lane()], [_host(FRESH)], {"alpha-2": (0, "dead\n")}, NOW) == {
        "alpha": {"alive": False, "last_beat_at": LANE_BEAT}
    }


def test_a_live_pid_on_a_fresh_host_is_alive_true():
    assert pid_probe_of([_lane()], [_host(FRESH)], {"alpha-2": (0, "alive\n")}, NOW) == {
        "alpha": {"alive": True, "last_beat_at": LANE_BEAT}
    }


def test_a_stale_host_has_no_entry():
    assert pid_probe_of([_lane()], [_host(STALE)], {"alpha-2": (0, "dead\n")}, NOW) == {}


def test_a_failed_probe_has_no_entry():
    host = [_host(FRESH)]
    assert pid_probe_of([_lane()], host, {"alpha-2": None}, NOW) == {}
    assert pid_probe_of([_lane()], host, {"alpha-2": (255, "dead\n")}, NOW) == {}
    assert pid_probe_of([_lane()], host, {"alpha-2": (3, "")}, NOW) == {}
    assert pid_probe_of([_lane()], host, {"alpha-2": (0, "Warning: permanently added")}, NOW) == {}
    assert pid_probe_of([_lane()], host, {}, NOW) == {}


def test_a_lane_on_an_unknown_host_has_no_entry():
    assert pid_probe_of([_lane(host="gone")], [_host(FRESH)], {"alpha-2": (0, "dead\n")}, NOW) == {}


def test_a_dead_pid_silent_for_eleven_minutes_on_a_fresh_host_is_marked_lost_by_the_plan():
    probe = pid_probe_of([_lane(beat="2026-10-05T11:49:00Z")], [_host(FRESH)], {"alpha-2": (0, "dead\n")}, NOW)
    facts = {
        "pid_probe": probe,
        "initiatives": [{"id": "alpha"}],
        "stall_candidates": [{"initiative": "alpha", "run": "alpha-2", "local": False}],
        "remote_unfetched": {},
        "run_exited": {},
        "lost_runs": {},
    }
    marked = _with_dead_pid_lost(facts, datetime(2026, 10, 5, 12, 0, tzinfo=UTC))
    assert marked["lost_runs"] == {"alpha": "alpha-2"}


def test_a_dead_pid_whose_lane_beat_is_recent_does_not_meet_the_plans_lost_rule():
    probe = pid_probe_of([_lane()], [_host(FRESH)], {"alpha-2": (0, "dead\n")}, NOW)
    assert _silent_dead_pid(probe["alpha"], datetime(2026, 10, 5, 12, 0, tzinfo=UTC)) is False


def test_the_newest_lane_of_an_initiative_wins_whatever_the_order():
    old = _lane("alpha-1", launched="2026-10-05T09:00:00Z")
    new = _lane("alpha-2", launched="2026-10-05T10:00:00Z")
    outputs = {"alpha-1": (0, "dead\n"), "alpha-2": (0, "alive\n")}
    expected = {"alpha": {"alive": True, "last_beat_at": LANE_BEAT}}
    assert pid_probe_of([new, old], [_host(FRESH)], outputs, NOW) == expected
    assert pid_probe_of([old, new], [_host(FRESH)], outputs, NOW) == expected


def _check(tmp_path: Path, content: str | None) -> tuple[int, str]:
    pidfile = tmp_path / "x.pid"
    if content is not None:
        pidfile.write_text(content)
    done = subprocess.run(pid_check_argv(str(pidfile)), capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def test_the_pid_check_script_run_locally(tmp_path):
    assert _check(tmp_path, None) == (3, "")
    assert _check(tmp_path, f"{os.getpid()}\n") == (0, "alive")
    assert _check(tmp_path, "999999999\n") == (0, "dead")
    assert _check(tmp_path, "abc\n") == (3, "")
    assert _check(tmp_path, "0\n") == (3, "")
    assert _check(tmp_path, "") == (3, "")


def test_the_pidfile_is_under_the_hosts_runs_dir():
    assert pidfile_path("/ws/", "alpha-2") == "/ws/runs/alpha-2.pid"


def _fake_store(monkeypatch, hosts: list[dict], lanes: list[run_store.Lane], pidfiled: set[str] = frozenset()):
    monkeypatch.setattr(chair_pid_probe.run_store, "hosts", lambda runs_dir: hosts)
    monkeypatch.setattr(chair_pid_probe.run_store, "live_lanes", lambda runs_dir, now: lanes)
    monkeypatch.setattr(chair_pid_probe, "local_runs", lambda runs_dir, now: (0, set(pidfiled)))


def test_the_edge_dials_a_fresh_host_over_ssh_and_never_a_stale_one(monkeypatch):
    _fake_store(
        monkeypatch,
        [_host(FRESH), _host(STALE, "old")],
        [_lane("alpha-2", "h"), _lane("beta-1", "old")],
    )
    dialled: list[list[str]] = []

    def fake_ssh(argv: list[str]):
        dialled.append(argv)
        return (0, "alive\n")

    assert read_pid_probe(Path("runs"), NOW, fake_ssh) == {"alpha": {"alive": True, "last_beat_at": LANE_BEAT}}
    assert len(dialled) == 1
    assert dialled[0][:2] == ["ssh", "me@h"]
    assert "/ws/runs/alpha-2.pid" in dialled[0][2]


def test_the_edge_skips_a_run_a_local_pidfile_names(monkeypatch):
    _fake_store(monkeypatch, [_host(FRESH)], [_lane("alpha-2")], pidfiled={"alpha-2"})
    dialled: list[list[str]] = []

    def fake_ssh(argv: list[str]):
        dialled.append(argv)
        return (0, "dead\n")

    assert read_pid_probe(Path("runs"), NOW, fake_ssh) == {}
    assert dialled == []


def test_the_edge_leaves_out_a_run_whose_ssh_failed(monkeypatch):
    _fake_store(monkeypatch, [_host(FRESH)], [_lane("alpha-2")])
    assert read_pid_probe(Path("runs"), NOW, lambda argv: None) == {}


def test_the_edge_takes_the_workspace_from_the_profile_when_the_host_never_recorded_one(monkeypatch):
    bare = {"name": "h", "beat_at": FRESH, "ssh": "me@h", "state": "active", "versions_json": "{}"}
    _fake_store(monkeypatch, [bare], [_lane("alpha-2")])
    dialled: list[list[str]] = []

    def fake_ssh(argv: list[str]):
        dialled.append(argv)
        return (0, "dead\n")

    assert read_pid_probe(Path("runs"), NOW, fake_ssh, (LaneHost("h", "me@h", "/prof"),)) == {
        "alpha": {"alive": False, "last_beat_at": LANE_BEAT}
    }
    assert "/prof/runs/alpha-2.pid" in dialled[0][2]
    assert read_pid_probe(Path("runs"), NOW, fake_ssh) == {}


def test_the_real_chair_bundle_feeds_the_probe_its_runs_dir_ssh_edge_and_local_host(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def fake_read(runs_dir, now, run_ssh, profile_hosts, local):
        calls.append((runs_dir, now, run_ssh, profile_hosts, local))
        return {"alpha": {"alive": False, "last_beat_at": LANE_BEAT}}

    monkeypatch.setattr(cli.chair_pid_probe, "read_pid_probe", fake_read)
    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", False, print, tmp_path / "p.yaml", "files")
    assert deps.facts_deps.pid_probe() == {"alpha": {"alive": False, "last_beat_at": LANE_BEAT}}
    [(runs_dir, now, run_ssh, profile_hosts, local)] = calls
    assert (runs_dir, run_ssh, tuple(profile_hosts), local) == (tmp_path, cli._pid_probe_ssh, (), "h")
    assert datetime.fromisoformat(now).tzinfo == UTC and now.endswith("Z")


def test_the_real_chair_bundle_probes_with_the_profiles_lane_hosts(monkeypatch, tmp_path):
    profile = tmp_path / "p.yaml"
    profile.write_text("lane_hosts:\n  - name: jarvis\n    ssh: me@jarvis\n    workspace_dir: /ws\n")
    calls: list[tuple] = []
    monkeypatch.setattr(cli.chair_pid_probe, "read_pid_probe", lambda *args: calls.append(args) or {})
    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.pid_probe() == {}
    assert tuple(calls[0][3]) == (LaneHost("jarvis", "me@jarvis", "/ws"),)


def test_the_real_chair_bundle_probes_with_no_profile_hosts_when_lane_hosts_is_malformed(monkeypatch, tmp_path):
    profile = tmp_path / "p.yaml"
    profile.write_text("lane_hosts: not-a-list\n")
    calls: list[tuple] = []
    monkeypatch.setattr(cli.chair_pid_probe, "read_pid_probe", lambda *args: calls.append(args) or {})
    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", False, print, profile, "files")
    assert deps.facts_deps.pid_probe() == {}
    assert tuple(calls[0][3]) == ()


def test_the_cli_ssh_edge_never_prompts_and_reads_a_timeout_as_no_answer(monkeypatch):
    seen: list[tuple[list[str], float]] = []

    def fake_run(argv, capture_output, text, timeout):
        seen.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, "dead\n", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli._pid_probe_ssh(["ssh", "me@h", "bash -c x"]) == (0, "dead\n")
    assert seen == [(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "me@h", "bash -c x"], 15)]

    def slow_run(argv, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(cli.subprocess, "run", slow_run)
    assert cli._pid_probe_ssh(["ssh", "me@h", "bash -c x"]) is None
