import json

from agent_tools import route_drift
from agent_tools.cli import main


def _row(initiative, task_id, state):
    return {"kind": "task", "initiative": initiative, "task_id": task_id, "state": state}


def _intake_row(task_id, state):
    return {"kind": "intake", "initiative": "intake", "task_id": task_id, "state": state}


def test_equal_states_give_nothing():
    assert route_drift.drift([("demo", "t1", "ready")], [_row("demo", "t1", "ready")]) == []


def test_differing_states_give_a_state_kind():
    assert route_drift.drift([("demo", "t1", "ready")], [_row("demo", "t1", "done")]) == [
        {"initiative": "demo", "task_id": "t1", "kind": "state", "file_state": "ready", "store_state": "done"}
    ]


def test_a_file_absent_from_the_store_is_file_only():
    out = route_drift.drift(
        [("demo", "t1", "ready")], [_row("other", "t9", "done")], [("intake/foo.md", "queued")], []
    )
    assert out[0] == {"initiative": "demo", "task_id": "t1", "kind": "file_only", "file_state": "ready", "store_state": None}
    assert out[1] == {
        "initiative": "intake", "task_id": "intake/foo.md", "kind": "file_only", "file_state": "queued", "store_state": None
    }


def test_a_store_row_absent_from_files_is_store_only():
    assert route_drift.drift([], [_row("demo", "t1", "done")], [], [_intake_row("foo", "landed")]) == [
        {"initiative": "demo", "task_id": "t1", "kind": "store_only", "file_state": None, "store_state": "done"},
        {"initiative": "intake", "task_id": "intake/foo.md", "kind": "store_only", "file_state": None, "store_state": "landed"},
    ]


def test_intake_equal_states_give_nothing():
    assert route_drift.drift([], [], [("intake/foo.md", "queued")], [_intake_row("foo", "queued")]) == []


def test_intake_differing_states_give_a_state_kind():
    assert route_drift.drift([], [], [("intake/foo.md", "queued")], [_intake_row("foo", "decomposed")]) == [
        {
            "initiative": "intake", "task_id": "intake/foo.md", "kind": "state",
            "file_state": "queued", "store_state": "decomposed",
        }
    ]


def test_output_is_sorted_by_initiative_then_task():
    out = route_drift.drift([("b", "t1", "todo"), ("a", "t2", "todo")], [_row("a", "t1", "done")])
    assert [(d["initiative"], d["task_id"]) for d in out] == [("a", "t1"), ("a", "t2"), ("b", "t1")]


def test_json_is_the_list_with_sorted_keys():
    d = {"initiative": "demo", "task_id": "t1", "kind": "state", "file_state": "ready", "store_state": "done"}
    i = {
        "initiative": "intake", "task_id": "intake/foo.md", "kind": "state",
        "file_state": "queued", "store_state": "decomposed",
    }
    assert json.loads(route_drift.format_json([d, i])) == [d, i]
    assert route_drift.format_json([]) == "[]"


def test_text_is_a_table_and_says_no_drift_when_empty():
    d = {"initiative": "demo", "task_id": "t1", "kind": "file_only", "file_state": "ready", "store_state": None}
    lines = route_drift.format_text([d]).splitlines()
    assert lines[0].split() == ["initiative", "task_id", "kind", "file_state", "store_state"]
    assert lines[1].split() == ["demo", "t1", "file_only", "ready", "-"]
    assert route_drift.format_text([]) == "no drift"


def _workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "work" / "demo" / "1-build").mkdir(parents=True)
    (ws / "work" / "demo" / "1-build" / "task.md").write_text("---\nstate: ready\n---\n\nDo the task\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    return profile


def _store(monkeypatch, rows):
    monkeypatch.setattr("agent_tools.cli.run_store.read_queue", lambda *_a, **_k: rows)


def test_edge_reports_drift_and_exits_zero(tmp_path, monkeypatch, capsys):
    _store(monkeypatch, [_row("demo", "task", "done")])
    rc = main(["route", "drift", "--profile", str(_workspace(tmp_path))])
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert lines[1].split() == ["demo", "task", "state", "ready", "done"]


def test_edge_json_prints_the_drift_list(tmp_path, monkeypatch, capsys):
    _store(monkeypatch, [_row("demo", "task", "done")])
    rc = main(["route", "drift", "--profile", str(_workspace(tmp_path)), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [
        {"initiative": "demo", "task_id": "task", "kind": "state", "file_state": "ready", "store_state": "done"}
    ]


def test_edge_with_no_drift_says_so_and_exits_zero(tmp_path, monkeypatch, capsys):
    _store(monkeypatch, [_row("demo", "task", "ready")])
    rc = main(["route", "drift", "--profile", str(_workspace(tmp_path))])
    assert (rc, capsys.readouterr().out) == (0, "no drift\n")


def test_edge_with_an_empty_store_prints_one_note_and_exits_zero(tmp_path, monkeypatch, capsys):
    _store(monkeypatch, [])
    rc = main(["route", "drift", "--profile", str(_workspace(tmp_path))])
    assert (rc, capsys.readouterr().out) == (0, "store has no work_items\n")


def test_edge_reports_intake_drift(tmp_path, monkeypatch, capsys):
    profile = _workspace(tmp_path)
    ws = tmp_path / "workspace"
    (ws / "intake").mkdir()
    (ws / "intake" / "foo.md").write_text("---\ntitle: Foo\n---\n\nDo the foo\n")
    _store(monkeypatch, [_row("demo", "task", "ready"), _intake_row("foo", "decomposed")])
    rc = main(["route", "drift", "--profile", str(profile)])
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert any(line.split() == ["intake", "intake/foo.md", "state", "queued", "decomposed"] for line in lines[1:])
