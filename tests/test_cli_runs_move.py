import json

from agent_tools import chair_exec, cli, runs_stop, store_cli
from agent_tools.cli import main
from agent_tools.lane_hosts import LaneHost

RUN = "demo-1"
HOSTS = (LaneHost(name="jarvis", ssh="jarvis.local", workspace_dir="/home/agent/ws"),)
CONFLICT = {"kind": "needs_chair", "initiative": "demo", "cause": "carry_conflict", "reason": "phase 2-core conflicts with main: a.py"}


def _patch_hosts(monkeypatch):
    """No real profile is read: the `--to` lookup goes through this fixed table instead."""
    monkeypatch.setattr(cli, "_runs_stop_hosts", lambda a: HOSTS)


def _patch_edges(monkeypatch, *, stop_ok=True, stop_reason="", clear_extra=None):
    """Fakes the pause, stop, clear_branches and relaunch edges; returns the call-order list."""
    order: list[str] = []

    def fake_pause(run_id, reason=None):
        order.append("pause")
        return store_cli.Paused(reason=reason)

    def fake_stop(run_id, runs_dir, **kwargs):
        order.append("stop")
        return {"ok": stop_ok, "reason": stop_reason}

    def fake_perform(actions, deps, current_epoch, dry_run):
        kind = actions[0]["kind"]
        order.append(kind)
        extra = clear_extra if kind == "clear_branches" and clear_extra else {}
        return [{"action": actions[0], "status": "done", "reason": "", **extra}]

    monkeypatch.setattr(store_cli, "pause", fake_pause)
    monkeypatch.setattr(runs_stop, "stop_run", fake_stop)
    monkeypatch.setattr(chair_exec, "perform", fake_perform)
    return order


def _argv(tmp_path, run_id, host, *extra):
    # a profile path that cannot exist keeps `_runs_move_harness_python` off the real filesystem
    return ["runs", "move", run_id, "--to", host, "--profile", str(tmp_path / "no-such-profile.yaml"), *extra]


def test_happy_path_moves_in_order(tmp_path, monkeypatch, capsys):
    _patch_hosts(monkeypatch)
    order = _patch_edges(monkeypatch)
    code = main(_argv(tmp_path, RUN, "jarvis"))
    out = capsys.readouterr().out
    assert out == f"moved {RUN} -> jarvis\n"
    assert code == 0
    assert order == ["pause", "stop", "clear_branches", "relaunch"]


def test_unknown_host_refuses_before_any_step(tmp_path, monkeypatch, capsys):
    _patch_hosts(monkeypatch)
    order = _patch_edges(monkeypatch)
    code = main(_argv(tmp_path, RUN, "ghost"))
    out = capsys.readouterr().out
    assert out == f"refused {RUN}: no such host ghost\n"
    assert code == 1
    assert order == []


def test_a_failing_stop_step_refuses_and_skips_the_rest(tmp_path, monkeypatch, capsys):
    _patch_hosts(monkeypatch)
    order = _patch_edges(monkeypatch, stop_ok=False, stop_reason="pidfile missing")
    code = main(_argv(tmp_path, RUN, "jarvis"))
    out = capsys.readouterr().out
    assert out == f"refused {RUN}: pidfile missing\n"
    assert code == 1
    assert order == ["pause", "stop"]


def test_a_done_clear_branches_carrying_a_needs_chair_refuses_before_relaunch(tmp_path, monkeypatch, capsys):
    _patch_hosts(monkeypatch)
    order = _patch_edges(monkeypatch, clear_extra={"needs_chair": CONFLICT})
    code = main(_argv(tmp_path, RUN, "jarvis"))
    out = capsys.readouterr().out
    assert out == f"refused {RUN}: clear_branches needs the chair: phase 2-core conflicts with main: a.py\n"
    assert code == 1
    assert order == ["pause", "stop", "clear_branches"]


def test_json_pairs_each_step_with_its_result_on_success_and_on_refusal(tmp_path, monkeypatch, capsys):
    _patch_hosts(monkeypatch)
    _patch_edges(monkeypatch)
    assert main(_argv(tmp_path, RUN, "jarvis", "--json")) == 0
    moved = json.loads(capsys.readouterr().out)
    assert (moved["ok"], moved["reason"]) == (True, "")
    assert [(e["step"]["kind"], e["ok"]) for e in moved["move_plan"]] == [
        ("pause", True), ("stop", True), ("clear_branches", True), ("relaunch", True),
    ]
    _patch_edges(monkeypatch, stop_ok=False, stop_reason="pidfile missing")
    assert main(_argv(tmp_path, RUN, "jarvis", "--json")) == 1
    refused = json.loads(capsys.readouterr().out)
    assert (refused["ok"], refused["reason"]) == (False, "pidfile missing")
    assert [(e["step"]["kind"], e["ok"]) for e in refused["move_plan"]] == [("pause", True), ("stop", False)]
