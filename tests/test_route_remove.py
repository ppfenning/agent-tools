"""`route remove` core: an intake moves to done, an initiative is dropped through decline's path, a live run refuses."""

from datetime import UTC, datetime
from pathlib import Path

from agent_tools import draft_apply, route_remove
from agent_tools.store_cli import StateSet

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
INTAKE_ROWS = [{"kind": "intake", "state": "queued", "task_id": "idea", "initiative": None}]
INITIATIVE_ROWS = [
    {"kind": "task", "initiative": "demo", "task_id": "a", "state": "todo"},
    {"kind": "task", "initiative": "demo", "task_id": "b", "state": "ready"},
]


def files_store() -> draft_apply.Store:
    return draft_apply.Store("files", lambda initiative: [], lambda *args: None)


def make_ws(tmp_path: Path) -> Path:
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "idea.md").write_text("---\ntitle: Idea\n---\nBody\n")
    root = tmp_path / "work" / "demo"
    (root / "1-build").mkdir(parents=True)
    (root / "initiative.md").write_text("---\ntitle: T\ndraft: false\n---\nProse\n")
    (root / "1-build" / "a.md").write_text("---\nstate: todo\n---\nA\n")
    (root / "1-build" / "b.md").write_text("---\nstate: ready\n---\nB\n")
    return tmp_path


def snapshot(ws: Path) -> dict[str, str]:
    return {str(p): p.read_text() for p in sorted(ws.rglob("*.md"))}


def run(ws: Path, id: str, rows: list[dict], live: tuple[str, ...] = (), dry_run: bool = False) -> int:
    return route_remove.remove(ws, id, "stale", "pat", NOW, rows, live, files_store(), dry_run=dry_run)


def test_the_removal_line_and_the_done_text():
    assert route_remove.removed_line("pat", "2026-10-05", "stale") == "removed by pat on 2026-10-05: stale"
    assert route_remove.intake_done_text("Body", "pat", "2026-10-05", "stale") == "Body\nremoved by pat on 2026-10-05: stale\n"


def test_remove_an_intake_moves_it_to_done_with_the_line(tmp_path):
    ws = make_ws(tmp_path)
    assert run(ws, "idea", INTAKE_ROWS) == 0
    assert not (ws / "intake" / "idea.md").exists()
    assert (ws / "intake" / "done" / "idea.md").read_text() == "---\ntitle: Idea\n---\nBody\nremoved by pat on 2026-10-05: stale\n"


def test_remove_an_initiative_drops_todo_and_ready_tickets(tmp_path):
    ws = make_ws(tmp_path)
    assert run(ws, "demo", INITIATIVE_ROWS) == 0
    demo = ws / "work" / "demo"
    assert [(demo / "1-build" / f"{t}.md").read_text() for t in "ab"] == ["---\nstate: dropped\n---\nA\n", "---\nstate: dropped\n---\nB\n"]
    text = (demo / "initiative.md").read_text()
    assert "declined_by: pat\n" in text
    assert 'declined_reason: "stale"\n' in text


def test_remove_on_a_live_run_is_refused_and_names_the_stop(tmp_path, capsys):
    ws = make_ws(tmp_path)
    before = snapshot(ws)
    assert run(ws, "demo", INITIATIVE_ROWS, live=("r1",)) == 2
    assert snapshot(ws) == before
    assert "cox runs stop r1" in capsys.readouterr().out


def test_dry_run_writes_nothing(tmp_path, capsys):
    ws = make_ws(tmp_path)
    before = snapshot(ws)
    assert run(ws, "demo", INITIATIVE_ROWS, dry_run=True) == 0
    assert run(ws, "idea", INTAKE_ROWS, dry_run=True) == 0
    assert snapshot(ws) == before
    assert "would remove" in capsys.readouterr().out


def test_store_mode_expects_the_real_old_state(tmp_path):
    ws, calls = make_ws(tmp_path), []
    rows = lambda initiative: [{"initiative": initiative, "task_id": t, "state": "todo"} for t in "ab"]  # noqa: E731
    store = draft_apply.Store("store", rows, lambda i, t, to, by, expected: calls.append((t, to, expected)) or StateSet({}))
    assert route_remove.remove(ws, "demo", "stale", "pat", NOW, INITIATIVE_ROWS, (), store) == 0
    assert calls == [("a", "dropped", "todo"), ("b", "dropped", "ready")]
