import pytest
from test_route_cli import _init_repo, _unmeasured_window, _write_harness, _write_launch_profile

from agent_tools import queue_export, usage_window
from agent_tools.cli import _export_board, main
from agent_tools.queue_rows import render_item


def _row(kind, initiative, task_id="", phase="", title="T"):
    return {
        "kind": kind, "initiative": initiative, "phase": phase, "task_id": task_id, "state": "ready",
        "title": title, "needs": [], "surfaces": [], "extra": {}, "body": f"{title} body\n",
    }


CONTENTLESS = {
    "kind": "task", "initiative": "x", "phase": "p1", "task_id": "x-t2", "state": "ready",
    "title": None, "needs": [], "surfaces": [], "extra": None, "body": None,
}
BOARD = [
    _row("initiative", "x", title="X"),
    _row("task", "x", "x-t1", "p1", "X one"),
    _row("initiative", "y", title="Y"),
    _row("task", "y", "y-t1", "p1", "Y one"),
]


@pytest.fixture(autouse=True)
def _no_usage_gather(monkeypatch):
    monkeypatch.setattr(usage_window, "gather", _unmeasured_window)


def _setup(tmp_path, monkeypatch, mode, launch_code=0, board=BOARD, stub_export=True):
    """Stub the store edges and `_merge_initiative_tickets`; return the shared call log, workspace and argv."""
    calls = []
    monkeypatch.setattr("agent_tools.work_state.work_state_mode", lambda _provider: mode)
    monkeypatch.setattr(
        "agent_tools.route.launch_from_rows",
        lambda runs_dir, initiative_id, repo: (
            calls.append(("launch_from_rows", runs_dir, initiative_id, repo))
            or (launch_code, ["routing: stub refusal"], None)
        ),
    )
    monkeypatch.setattr(
        "agent_tools.run_store.read_queue",
        lambda runs_dir, initiative=None, kind=None: calls.append(("read_queue", initiative)) or board,
    )
    real_export = queue_export.export_files
    monkeypatch.setattr(
        "agent_tools.queue_export.export_files",
        lambda workspace, rows: calls.append(("export", workspace, rows))
        or (queue_export.Export([], []) if stub_export else real_export(workspace, rows)),
    )
    monkeypatch.setattr("agent_tools.cli._merge_initiative_tickets", lambda d: calls.append(("merge", d)) or [])

    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    initiative_dir = ws / "work" / "x"
    initiative_dir.mkdir(parents=True)
    (initiative_dir / "initiative.md").write_text("---\nid: x\ntitle: X\n---\n\nBody\n")
    repo = tmp_path / "repo"
    _init_repo(repo)
    profile = _write_launch_profile(tmp_path, _write_harness(tmp_path), ws)
    argv = [
        "route", "launch", "epic", "--dry-run", "--profile", str(profile),
        "--initiative", str(initiative_dir), "--repo", str(repo),
    ]
    return calls, ws, repo, argv


def test_store_state_exports_the_whole_board_before_the_launch_reads_it(tmp_path, monkeypatch):
    calls, ws, repo, argv = _setup(tmp_path, monkeypatch, "store")

    assert main(argv) == 0

    names = [c[0] for c in calls]
    assert names.index("launch_from_rows") < names.index("export") < names.index("merge")
    assert ("launch_from_rows", ws / "runs", "x", str(repo)) in calls
    assert ("read_queue", None) in calls
    assert ("export", ws, BOARD) in calls


def test_store_state_export_keeps_other_initiatives_tickets(tmp_path, monkeypatch):
    other = tmp_path / "workspace" / "work" / "y" / "p1" / "y-t1.md"
    other.parent.mkdir(parents=True)
    other.write_text("stale\n")
    _calls, ws, _repo, argv = _setup(tmp_path, monkeypatch, "store", stub_export=False)

    assert main(argv) == 0

    assert other.read_text() == render_item(BOARD[3])
    assert (ws / "work" / "x" / "p1" / "x-t1.md").exists()


def test_store_state_nonzero_code_prints_its_lines_returns_it_and_never_exports(tmp_path, monkeypatch, capsys):
    calls, _ws, _repo, argv = _setup(tmp_path, monkeypatch, "store", launch_code=2)

    assert main(argv) == 2

    assert "routing: stub refusal" in capsys.readouterr().out
    assert [c[0] for c in calls] == ["launch_from_rows"]


def test_files_state_calls_neither_launch_from_rows_nor_export(tmp_path, monkeypatch):
    calls, _ws, _repo, argv = _setup(tmp_path, monkeypatch, "files")

    assert main(argv) == 0

    assert [c[0] for c in calls] == ["merge"]


def test_contentless_row_is_skipped_and_does_not_abort_the_launch(tmp_path, monkeypatch, capsys):
    calls, ws, _repo, argv = _setup(tmp_path, monkeypatch, "store", board=[*BOARD, CONTENTLESS], stub_export=False)

    assert main(argv) == 0

    assert "skipped 1 contentless row(s): x-t2" in capsys.readouterr().out
    assert "merge" in [c[0] for c in calls]
    assert (ws / "work" / "x" / "p1" / "x-t1.md").exists()


def test_export_board_returns_the_skipped_ids(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_tools.run_store.read_queue", lambda runs_dir, initiative=None, kind=None: [*BOARD, CONTENTLESS])

    assert _export_board(tmp_path / "runs", tmp_path) == ["x-t2"]
    assert (tmp_path / "work" / "y" / "p1" / "y-t1.md").read_text() == render_item(BOARD[3])
