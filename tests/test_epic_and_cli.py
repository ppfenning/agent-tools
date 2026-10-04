import argparse
import json
import os
import time
import tomllib
from pathlib import Path

import pytest
from conftest import strip_ansi
from test_run_store import ROW, leases_table, run_row, runs_table, store

from agent_tools import cli as cli_module
from agent_tools import epic
from agent_tools.cli import build_parser, main

LOG = """
  quarantined task: a — reason
  reused b from run-1 (approved patch, no model call)
epic run-2: 1 phase(s) complete, 0 partial, 0 blocked, 1 task(s) quarantined, 0 stack(s) rebased
  usage   : 5 node call(s), 20 turns, $1.00
"""


def test_summarize_log_picks_the_outcome_lines():
    s = epic.summarize_log(LOG)
    assert s["quarantined"] == ["quarantined task: a — reason"] and s["reused"][0].startswith("reused b")
    assert s["summary"].startswith("epic run-2") and s["usage"].startswith("usage")


def test_watch_returns_at_once_for_a_dead_pid(tmp_path):
    pf = tmp_path / "pid"; pf.write_text("999999999")
    log = tmp_path / "log"; log.write_text(LOG)
    out = epic.watch(pf, log=log, max_seconds=1, interval=0.1)
    assert out["finished"] and out["summary"].startswith("epic run-2")


def test_proc_start_epoch_reads_field_22_after_a_comm_holding_parens():
    stat = "42 (a) (b c) S " + " ".join(["0"] * 18) + " 500 0"
    assert epic.proc_start_epoch(stat, 1000.0, 100) == 1005.0


def test_proc_start_epoch_is_none_for_a_line_without_the_field():
    assert epic.proc_start_epoch("garbage", 1000.0, 100) is None


LSTART = "Sun Oct  4 09:58:42 2026\n"
LSTART_EPOCH = time.mktime(time.strptime("2026-10-04 09:58:42", "%Y-%m-%d %H:%M:%S"))


def test_ps_lstart_epoch_reads_local_time():
    assert epic.ps_lstart_epoch(LSTART) == LSTART_EPOCH


def test_ps_lstart_epoch_is_none_for_empty_text():
    assert epic.ps_lstart_epoch("") is None


def test_start_epoch_falls_back_to_ps_when_proc_is_unreadable(tmp_path):
    assert epic._start_epoch(1, proc_root=str(tmp_path), ps=lambda pid: LSTART) == LSTART_EPOCH


def test_start_epoch_is_none_when_proc_and_ps_both_fail(tmp_path):
    assert epic._start_epoch(1, proc_root=str(tmp_path), ps=lambda pid: None) is None


def test_a_process_started_after_the_launch_is_a_reused_pid():
    assert epic.reused(started_at=200.0, launched_at=100.0)


def test_a_process_started_within_the_slack_of_the_launch_is_not_reused():
    assert not epic.reused(started_at=101.0, launched_at=100.0)


def test_a_live_pid_that_started_after_the_launch_is_not_alive(monkeypatch):
    monkeypatch.setattr(epic, "_start_epoch", lambda pid: 500.0)
    assert not epic.run_alive(os.getpid(), 100.0, None)


def test_a_live_pid_that_started_before_the_launch_is_alive(monkeypatch):
    monkeypatch.setattr(epic, "_start_epoch", lambda pid: 50.0)
    assert epic.run_alive(os.getpid(), 100.0, None)


def test_a_live_pid_with_no_readable_start_time_falls_back_to_the_bare_probe(monkeypatch):
    monkeypatch.setattr(epic, "_start_epoch", lambda pid: None)
    assert epic.run_alive(os.getpid(), 100.0, None)


def test_the_real_proc_start_time_of_this_process_is_after_an_epoch_zero_launch():
    assert not epic.run_alive(os.getpid(), 0.0, None)


def test_a_log_ending_in_the_summary_is_exited_whatever_the_pid_says(monkeypatch):
    monkeypatch.setattr(epic, "_start_epoch", lambda pid: 50.0)
    assert not epic.run_alive(os.getpid(), 100.0, LOG)


def test_a_log_without_the_summary_line_is_not_ended():
    assert not epic.log_ended("  reused b from run-1\n")


def test_launched_epoch_reads_the_at_field():
    assert epic.launched_epoch('{"at": "1970-01-01T00:01:40+00:00"}') == 100.0


def test_launched_epoch_is_none_for_a_missing_or_unreadable_record():
    assert [epic.launched_epoch(t) for t in (None, "{", "{}")] == [None, None, None]


def test_run_live_takes_the_launch_time_from_launched_json(tmp_path):
    (tmp_path / "r.pid").write_text(str(os.getpid()))
    (tmp_path / "r.launched.json").write_text(json.dumps({"at": "1970-01-01T00:01:40+00:00"}))
    assert not epic.run_live(os.getpid(), tmp_path / "r.pid")


def test_run_live_falls_back_to_the_pidfile_mtime_without_a_launched_json(tmp_path):
    pidfile = tmp_path / "r.pid"
    pidfile.write_text(str(os.getpid()))
    os.utime(pidfile, (100, 100))
    assert not epic.run_live(os.getpid(), pidfile)


def test_run_live_reads_the_log_beside_the_pidfile(tmp_path):
    (tmp_path / "r.pid").write_text(str(os.getpid()))
    (tmp_path / "r.log").write_text(LOG)
    assert not epic.run_live(os.getpid(), tmp_path / "r.pid")


def test_run_live_never_signals_pid_zero_or_a_pid_too_large_for_pid_t(tmp_path):
    assert [epic.run_live(p, tmp_path / "r.pid") for p in (None, 0, -1, 2**80)] == [False] * 4


def test_watch_reports_finished_for_a_reused_pid_whose_log_has_the_summary(tmp_path):
    pidfile = tmp_path / "r.pid"
    pidfile.write_text(str(os.getpid()))
    log = tmp_path / "r.log"
    log.write_text(LOG)
    assert epic.watch(pidfile, log=log, max_seconds=1, interval=0.1)["finished"]


def test_route_context_does_not_count_a_reused_pid_as_in_flight(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "runs").mkdir(parents=True)
    (ws / "runs" / "old-1.pid").write_text(str(os.getpid()))
    (ws / "runs" / "old-1.launched.json").write_text(json.dumps({"at": "2020-01-01T00:00:00+00:00"}))
    profile = tmp_path / "p.yaml"
    profile.write_text(f"team: t\nworkspace_dir: {ws}\n")
    cli_module.main(["route", "context", "--profile", str(profile), "--json"])
    assert json.loads(capsys.readouterr().out)["runs"][0]["alive"] is False


def test_runs_usage_does_not_call_a_reused_pid_live(tmp_path, capsys):
    (tmp_path / "r.pid").write_text(str(os.getpid()))
    (tmp_path / "r.log").write_text(LOG)
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="r", json=False))
    assert rc == 2 and "no usage record" in capsys.readouterr().out


def test_every_subcommand_parses():
    p = build_parser()
    for argv in (["runs", "usage", "r"], ["runs", "trace", "r", "--role", "build"], ["runs", "clean", "r", "--repo", "."],
                 ["epic", "watch", "pid"], ["plan", "serve", "d"]):
        assert p.parse_args(argv).fn


def test_runs_usage_recovers_totals_from_the_trace_when_live(tmp_path, monkeypatch, capsys):
    (tmp_path / "r.pid").write_text("4321")
    trace_dir = tmp_path / "r-trace"; trace_dir.mkdir()
    (trace_dir / "build-1.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": 1.5, "num_turns": 3}) + "\n")
    monkeypatch.setattr(cli_module.epic, "alive", lambda pid: True)
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="r", json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "live (pid 4321) — from the trace so far" in out
    assert "build" in out and "1.50" in out


def test_runs_usage_recovers_totals_from_a_trace_only_directory_not_live(tmp_path, capsys):
    trace_dir = tmp_path / "r-trace"; trace_dir.mkdir()
    (trace_dir / "build-1.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": 1.5, "num_turns": 3}) + "\n")
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="r", json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not live — from the trace so far" in out
    assert "live (pid" not in out
    assert "build" in out and "1.50" in out


def test_runs_usage_recovers_totals_as_json_stays_valid_json_on_stdout(tmp_path, capsys):
    trace_dir = tmp_path / "r-trace"; trace_dir.mkdir()
    (trace_dir / "build-1.jsonl").write_text(json.dumps({"type": "result", "total_cost_usd": 1.5, "num_turns": 3}) + "\n")
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="r", json=True))
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["note"] == "not live — from the trace so far"
    assert payload["by_role"]["build"]["cost_usd"] == 1.5


def test_runs_usage_with_no_usage_file_and_no_trace_exits_2(tmp_path, capsys):
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="ghost", json=False))
    out = capsys.readouterr().out
    assert rc == 2
    assert str(tmp_path) in out


def test_runs_usage_with_a_usage_file_present_is_unchanged(tmp_path, capsys):
    usage = {"run_id": "r2", "calls": [{"role": "build", "model": "m", "cost_usd": 2.0, "turns": 4}]}
    (tmp_path / "r2.usage.json").write_text(json.dumps(usage))
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="r2", json=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "r2: 1 calls, 4 turns, $2.00" in out
    assert "live (pid" not in out


def test_runs_usage_with_no_usage_file_reads_an_ended_run_from_the_store(tmp_path, capsys):
    store(tmp_path, {**ROW, "run_id": "r3", "role": "build", "model_alias": "opus", "cost_usd": 2.0, "turns": 4})
    runs_table(tmp_path, run_row("r3"))
    rc = cli_module._runs_usage(argparse.Namespace(runs_dir=str(tmp_path), run_id="r3", json=False))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "r3: 1 calls, 4 turns, $2.00" in out
    assert "no usage record" not in out


def test_cox_and_agent_tools_scripts_resolve_to_the_same_callable():
    data = tomllib.loads(Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts["cox"] == scripts["agent-tools"]


def test_help_usage_names_cox(capsys, monkeypatch):
    monkeypatch.setenv("PYTHON_COLORS", "0")
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert strip_ansi(capsys.readouterr().out).startswith("usage: cox")


NOW = "2026-09-25T05:00:00Z"
DEAD_PID = 999999999


def _lease_case(tmp_path, holder, expires_at):
    leases_table(tmp_path, ("runs:x", holder, expires_at))
    return tmp_path / "x-3.pid"


def test_run_live_is_live_on_a_held_lease_even_when_the_pid_is_dead(tmp_path):
    pidfile = _lease_case(tmp_path, "x-3", "2026-09-25T06:00:00Z")
    assert epic.run_live(DEAD_PID, pidfile, now=NOW)


@pytest.mark.parametrize("holder, expires_at", [
    ("x-3", "2026-09-25T04:00:00Z"),
    ("x-3", "1970-01-01T00:00:00Z"),
    ("x-4", "2026-09-25T06:00:00Z"),
])
def test_run_live_is_not_live_on_an_expired_released_or_newer_lease_even_when_the_pid_answers(tmp_path, holder, expires_at):
    pidfile = _lease_case(tmp_path, holder, expires_at)
    assert not epic.run_live(os.getpid(), pidfile, now=NOW)


def test_run_live_falls_back_to_the_pid_when_the_store_has_no_row_for_the_run(tmp_path):
    leases_table(tmp_path, ("runs:y", "y-1", "2026-09-25T06:00:00Z"))
    assert epic.run_live(os.getpid(), tmp_path / "x-3.pid", now=NOW)
    assert not epic.run_live(DEAD_PID, tmp_path / "x-3.pid", now=NOW)


def test_summarize_log_returns_the_approved_but_not_landed_lines():
    s = epic.summarize_log("  approved but not landed: t1 (run-2)\nepic run-2: done\n")
    assert s["approved"] == ["approved but not landed: t1 (run-2)"] and s["summary"] == "epic run-2: done"


def _lanes(tmp_path, monkeypatch, live_calls):
    """Two pidfiles; `live_calls` maps a run to how many probes it answers live before it reads dead."""
    for run in live_calls:
        (tmp_path / f"{run}.pid").write_text("1")
    (tmp_path / "b.log").write_text("approved but not landed: t9\nepic b: done\n")
    seen = dict.fromkeys(live_calls, 0)

    def fake(pid, pidfile, log=None, now=None):
        seen[pidfile.stem] += 1
        return seen[pidfile.stem] <= live_calls[pidfile.stem]

    monkeypatch.setattr(epic, "run_live", fake)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)


def test_runs_wait_prints_the_run_that_exited_with_its_approved_line(tmp_path, monkeypatch, capsys):
    _lanes(tmp_path, monkeypatch, {"a": 99, "b": 2})
    assert main(["runs", "wait", "--runs-dir", str(tmp_path), "--interval", "0"]) == 0
    out = capsys.readouterr().out
    assert "exited b at " in out and "approved but not landed: t9" in out and "exited a" not in out


def test_runs_wait_json_lists_the_exited_and_the_still_busy(tmp_path, monkeypatch, capsys):
    _lanes(tmp_path, monkeypatch, {"a": 99, "b": 2})
    assert main(["runs", "wait", "--runs-dir", str(tmp_path), "--interval", "0", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["busy"] == ["a"] and got["exited"][0]["run"] == "b" and got["exited"][0]["approved"] == ["approved but not landed: t9"]


def test_runs_wait_with_nothing_busy_exits_2(tmp_path, monkeypatch, capsys):
    _lanes(tmp_path, monkeypatch, {"a": 0, "b": 0})
    assert main(["runs", "wait", "--runs-dir", str(tmp_path)]) == 2
    assert capsys.readouterr().out.strip() == "all lanes clear"


def test_runs_wait_times_out_with_exit_3(tmp_path, monkeypatch, capsys):
    _lanes(tmp_path, monkeypatch, {"a": 99, "b": 99})
    assert main(["runs", "wait", "--runs-dir", str(tmp_path), "--max-seconds", "0"]) == 3
    assert capsys.readouterr().out.strip() == "still busy: a b"


def test_wait_lanes_sleeps_the_interval_between_polls_on_a_fake_clock():
    clock, slept = [0.0], []
    polls = iter([True, True, False])

    def sleep(s):
        slept.append(s)
        clock[0] += s

    assert epic.wait_lanes(["r"], lambda _r: next(polls), lambda: clock[0], sleep, 100, 15) == (["r"], [])
    assert slept == [15, 15]
