import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_tools import chair_exec, chair_report, cli
from agent_tools.chair_run import RunDeps, changed_row_ids, tick

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)
FACTS = {
    "lease": {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False},
    "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
    "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
    "initiatives": [],
}


def _result(status, **action):
    return {"action": {"kind": "land", "epoch": 3, **action}, "status": status, "reason": ""}


class Recorder:
    def __init__(self, note="", raises=None):
        self.calls, self.note, self.raises = [], note, raises

    def __call__(self, ids):
        self.calls.append(ids)
        if self.raises is not None:
            raise self.raises
        return self.note


def _deps(results, hook) -> RunDeps:
    exec_deps = chair_exec.Deps(
        run=lambda argv: (0, ""), delete_branches=lambda repo, pattern: ([], ""), acquire_lease=lambda holder, host: "",
        record=lambda row: None, run_id=lambda action: "run-1", repo_for=lambda action: "r",
    )
    return RunDeps(
        facts_deps=object(), exec_deps=exec_deps, report_deps=chair_report.Deps(echo=lambda line: None),
        beat=lambda: None, current_epoch=lambda: 3, holds=lambda: True, release=lambda: None, sleep=lambda s: None,
        now=lambda: NOW, gather=lambda deps, now: FACTS, plan=lambda facts, now: [{"kind": "land", "epoch": 3}],
        perform=lambda actions, exec_deps, epoch, dry_run: results, export_rows=hook,
    )


def test_changed_row_ids_takes_done_landed_and_recorded_results_in_first_seen_order():
    results = [
        _result("landed", task_id="t2"),
        _result("failed", task_id="t9"),
        _result("done", task_id="t1", stale_tasks=["t2", "t3"]),
        _result("recorded", intake_ids=["i1"]),
    ]
    assert changed_row_ids(results) == ("t2", "t1", "t3", "i1")


def test_changed_row_ids_is_empty_when_no_result_changed_a_row():
    assert changed_row_ids([_result("skipped", task_id="t1"), _result("dry_run", task_id="t2")]) == ()


def test_a_tick_that_changed_rows_calls_the_hook_once_with_those_ids():
    hook = Recorder()
    tick(_deps([_result("landed", task_id="t1"), _result("done", task_id="t2")], hook), False, NOW)
    assert hook.calls == [("t1", "t2")]


def test_a_tick_that_changed_nothing_does_not_call_the_hook():
    hook = Recorder()
    tick(_deps([_result("skipped", task_id="t1")], hook), False, NOW)
    assert hook.calls == []


def test_a_dry_run_does_not_call_the_hook():
    hook = Recorder()
    tick(_deps([_result("done", task_id="t1")], hook), True, NOW)
    assert hook.calls == []


def test_a_hook_note_is_appended_to_the_line():
    line = tick(_deps([_result("landed", task_id="t1")], Recorder(note="export: committed 1 row(s)")), False, NOW)
    assert line.endswith(" | export: committed 1 row(s)")


def test_a_raising_hook_is_reported_and_the_tick_still_names_its_changes():
    hook = Recorder(raises=RuntimeError("git commit: boom"))
    line = tick(_deps([_result("landed", task_id="t1")], hook), False, NOW)
    assert "lands 1" in line and line.endswith(" | export failed: RuntimeError: git commit: boom")


def test_a_failure_line_returned_by_the_hook_is_reported_beside_the_changes():
    line = tick(_deps([_result("landed", task_id="t1")], Recorder(note="export failed: disk full")), False, NOW)
    assert "lands 1" in line and line.endswith(" | export failed: disk full")


def test_a_hook_with_no_note_leaves_the_line_as_it_is_with_no_hook():
    deps = _deps([_result("landed", task_id="t1")], None)
    assert tick(replace(deps, export_rows=Recorder()), False, NOW) == tick(deps, False, NOW)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_the_cli_hook_commits_the_exported_files_with_the_row_count(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    runs = tmp_path / "runs"
    runs.mkdir()

    def export(runs_dir, workspace):
        (workspace / "work").mkdir(exist_ok=True)
        (workspace / "work" / "a.md").write_text("a\n")
        return ["x1"]

    monkeypatch.setattr(cli, "_export_board", export)
    from agent_tools import store_fill
    monkeypatch.setattr(store_fill, "fill_workspace", lambda ws: "inserted 0, filled 0, unchanged 0, skipped 0")
    assert cli._chair_export_hook(runs, ("t1", "t2")) == "export: committed 2 row(s); skipped 1 contentless row(s): x1"
    assert _git(tmp_path, "log", "-1", "--format=%s") == "chair: export 2 changed row(s)"
    assert cli._chair_export_hook(runs, ("t1",)) == "export: no file changes; skipped 1 contentless row(s): x1"
    assert _git(tmp_path, "rev-list", "--count", "HEAD") == "1"


def test_export_board_fills_the_store_from_files_before_it_exports(tmp_path, monkeypatch):
    from agent_tools import store_fill
    order = []
    monkeypatch.setattr(store_fill, "fill_workspace", lambda ws: order.append(("fill", ws)) or "")
    monkeypatch.setattr(cli.run_store, "read_queue", lambda runs_dir: order.append(("read", runs_dir)) or [])
    monkeypatch.setattr(cli.queue_export, "export_files", lambda ws, rows: order.append(("export", ws)) or SimpleNamespace(skipped=[]))
    cli._export_board(tmp_path / "runs", tmp_path)
    assert order == [("fill", tmp_path), ("read", tmp_path / "runs"), ("export", tmp_path)]


def test_a_failed_fill_stops_the_export_before_anything_is_written(tmp_path, monkeypatch):
    from agent_tools import store_fill

    def unreadable(ws):
        raise RuntimeError("store-fill: the store could not be read; nothing written")

    exported = []
    monkeypatch.setattr(store_fill, "fill_workspace", unreadable)
    monkeypatch.setattr(cli.queue_export, "export_files", lambda ws, rows: exported.append(ws))
    with pytest.raises(RuntimeError, match="could not be read"):
        cli._export_board(tmp_path / "runs", tmp_path)
    assert exported == []
