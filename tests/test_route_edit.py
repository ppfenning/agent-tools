import pytest

from agent_tools import route_edit, run_store

INTAKE = "---\nid: fix-it\ntitle: Old title\nrepo: r\n---\n\nOld body\n"
INITIATIVE = "---\nid: ship\ntitle: Old title\nrepo: r\n---\n\nOld body\n"


def _queue(monkeypatch, rows, stored=True):
    upserts = []
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir, initiative=None, kind=None: rows)
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: upserts.append(row) or stored)
    return upserts


def _intake_row():
    return {"kind": "intake", "initiative": "intake", "task_id": "fix-it", "state": "queued", "extra": {}}


def _task_row(state):
    return {"kind": "task", "initiative": "ship", "task_id": "ship", "state": state}


def _workspace(tmp_path, relative, text):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_edit_an_intake_rewrites_the_file_and_the_store_row(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", title="New: title", live_runs=())
    assert result.code == 0
    assert '-title: Old title\n+title: "New: title"\n' in result.text
    assert path.read_text(encoding="utf-8") == INTAKE.replace("title: Old title", 'title: "New: title"')
    assert [row["title"] for row in upserts] == ["New: title"]


def test_edit_an_initiative_rewrites_initiative_md(tmp_path, monkeypatch):
    _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", body="New body", repo="other", live_runs=())
    assert result.code == 0
    assert "+++ b/work/ship/initiative.md" in result.text
    assert path.read_text(encoding="utf-8") == "---\nid: ship\ntitle: Old title\nrepo: other\n---\n\nNew body\n"


def test_edit_refused_on_a_live_run_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", title="New", live_runs=["R1"])
    assert result.code == 2
    assert "run R1 is live" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert upserts == []


def test_dry_run_returns_the_diff_and_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", title="New", dry_run=True, live_runs=())
    assert result.code == 0
    assert "-title: Old title\n+title: New\n" in result.text
    assert path.read_text(encoding="utf-8") == INTAKE
    assert upserts == []


def test_initiative_dry_run_writes_nothing(tmp_path, monkeypatch):
    upserts = _queue(monkeypatch, [_task_row("ready")])
    path = _workspace(tmp_path, "work/ship/initiative.md", INITIATIVE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "ship", title="New", dry_run=True, live_runs=())
    assert result.code == 0
    assert "+title: New\n" in result.text
    assert path.read_text(encoding="utf-8") == INITIATIVE
    assert upserts == []


def test_live_runs_is_required(tmp_path, monkeypatch):
    _queue(monkeypatch, [_task_row("ready")])
    with pytest.raises(TypeError):
        route_edit.apply_edit(tmp_path, tmp_path, "ship", title="New")


def test_body_file_trailing_newline_adds_no_blank_line(tmp_path, monkeypatch):
    _queue(monkeypatch, [_intake_row()])
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    source = tmp_path / "body.txt"
    source.write_text("New body\n", encoding="utf-8")
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", body_file=str(source), live_runs=())
    assert result.code == 0
    assert path.read_text(encoding="utf-8") == INTAKE.replace("Old body", "New body")


def test_store_down_warns_and_still_writes_the_intake_file(tmp_path, monkeypatch, capsys):
    _queue(monkeypatch, [_intake_row()], stored=False)
    path = _workspace(tmp_path, "intake/fix-it.md", INTAKE)
    result = route_edit.apply_edit(tmp_path, tmp_path, "fix-it", title="New", live_runs=())
    assert result.code == 0
    assert "store unavailable, filing" in capsys.readouterr().out
    assert "title: New" in path.read_text(encoding="utf-8")
