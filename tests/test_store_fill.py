import argparse

from agent_tools import run_store, store_fill

_TASK = {
    "kind": "task", "initiative": "demo", "task_id": "t1", "phase": "p1", "state": "ready", "needs": [],
    "title": "Do it", "surfaces": [], "body": "\nbody\n", "extra": {"id": "t1"},
}
_INITIATIVE = {
    "kind": "initiative", "initiative": "demo", "task_id": "demo", "phase": "", "state": "todo", "needs": [],
    "title": "Demo", "surfaces": [], "body": "\nwhy\n", "extra": {"id": "demo"},
}
_STATE_ONLY = {
    "kind": None, "initiative": "demo", "task_id": "t1", "phase": None, "state": "done", "needs": ["t0"],
    "title": None, "surfaces": None, "body": None, "extra": None, "priority": 3,
    "holder": None, "epoch": None, "expires_at": None,
}


def _apply(existing, writes):
    by_key = {(r["initiative"], r["task_id"]): r for r in existing}
    return list({**by_key, **{(w["initiative"], w["task_id"]): w for w in writes}}.values())


def test_a_missing_row_is_inserted():
    plan = store_fill.plan_fill([_TASK], [])
    assert (plan.writes, plan.inserted, plan.filled, plan.unchanged) == ((_TASK,), 1, 0, 0)


def test_a_contentless_row_is_filled_and_keeps_its_state_needs_and_priority():
    plan = store_fill.plan_fill([_TASK], [_STATE_ONLY])
    assert (plan.inserted, plan.filled, plan.unchanged) == (0, 1, 0)
    (write,) = plan.writes
    assert (write["state"], write["needs"], write["priority"]) == ("done", ["t0"], 3)
    assert (write["kind"], write["title"], write["body"], write["extra"]) == ("task", "Do it", "\nbody\n", {"id": "t1"})
    assert "holder" not in write


def test_present_content_is_never_overwritten():
    stored = {**_TASK, "title": "Edited in the store", "state": "approved"}
    assert store_fill.plan_fill([_TASK], [stored]).writes == ()


def test_an_initiative_row_keeps_kind_initiative():
    plan = store_fill.plan_fill([_INITIATIVE], [])
    assert plan.writes[0]["kind"] == "initiative"


def test_planning_over_its_own_result_writes_nothing():
    parsed = [_INITIATIVE, _TASK]
    first = store_fill.plan_fill(parsed, [_STATE_ONLY])
    again = store_fill.plan_fill(parsed, _apply([_STATE_ONLY], first.writes))
    assert (again.writes, again.inserted, again.filled, again.unchanged) == ((), 0, 0, 2)


def test_the_first_row_per_key_wins():
    queued, done = {**_TASK, "kind": "intake", "state": "queued"}, {**_TASK, "kind": "intake", "state": "done"}
    assert store_fill.plan_fill([queued, done], []).writes == (queued,)


def test_parse_workspace_builds_each_kind_and_skips_held_and_malformed_files():
    files = [
        (("work", "demo", "initiative.md"), "---\ntitle: Demo\n---\nwhy\n"),
        (("work", "demo", "p1", "t1.md"), "---\nstate: ready\ntitle: Do it\n---\nbody\n"),
        (("intake", "a.md"), "---\ntitle: A\n---\n"),
        (("intake", "done", "b.md"), "---\ntitle: B\n---\n"),
        (("intake", "held", "c.md"), "---\ntitle: C\n---\n"),
        (("work", "demo", "p1", "bad.md"), "no frontmatter\n"),
    ]
    rows, skipped = store_fill.parse_workspace(files)
    assert [(r["kind"], r["task_id"], r["state"]) for r in rows] == [
        ("initiative", "demo", "todo"), ("task", "t1", "ready"), ("intake", "a", "queued"), ("intake", "b", "done"),
    ]
    assert skipped == 2


def _workspace(tmp_path):
    (tmp_path / "work" / "demo" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "demo" / "initiative.md").write_text("---\ntitle: Demo\n---\nwhy\n")
    (tmp_path / "work" / "demo" / "p1" / "t1.md").write_text("---\nstate: ready\ntitle: Do it\n---\nbody\n")
    return argparse.Namespace(workspace=str(tmp_path), profile=None)


class _Conn:
    def __init__(self, count):
        self.count, self.closed = count, False

    def execute(self, sql):
        return self

    def fetchone(self):
        return {"n": self.count}

    def close(self):
        self.closed = True


def _forbid(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} must not run")
    return fail


def test_an_unreadable_store_plans_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    a = _workspace(tmp_path)
    monkeypatch.setattr(store_fill, "plan_fill", _forbid("plan_fill"))
    monkeypatch.setattr(run_store, "upsert_row_detail", _forbid("upsert_row_detail"))
    for opened in (lambda runs_dir: None, _forbid("connect_readonly")):
        monkeypatch.setattr(run_store, "connect_readonly", opened)
        assert store_fill.main(a) == 2
        assert capsys.readouterr().out == "store-fill: the store could not be read; nothing written\n"


def test_a_counted_store_whose_rows_do_not_read_is_unreadable(tmp_path, monkeypatch, capsys):
    a = _workspace(tmp_path)
    monkeypatch.setattr(run_store, "connect_readonly", lambda runs_dir: _Conn(5))
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [])
    monkeypatch.setattr(run_store, "upsert_row_detail", _forbid("upsert_row_detail"))
    assert store_fill.main(a) == 2
    assert "could not be read" in capsys.readouterr().out


def test_an_opened_empty_store_is_empty_and_every_row_is_inserted(tmp_path, monkeypatch, capsys):
    a, upserts = _workspace(tmp_path), []
    conn = _Conn(0)
    monkeypatch.setattr(run_store, "connect_readonly", lambda runs_dir: conn)
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [])
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: upserts.append(row) or "")
    assert store_fill.main(a) == 0
    assert capsys.readouterr().out == "inserted 2, filled 0, unchanged 0, skipped 0\n"
    assert [(r["kind"], r["task_id"]) for r in upserts] == [("initiative", "demo"), ("task", "t1")]
    assert conn.closed


def test_a_refused_upsert_stops_with_exit_2(tmp_path, monkeypatch, capsys):
    a = _workspace(tmp_path)
    monkeypatch.setattr(run_store, "connect_readonly", lambda runs_dir: _Conn(0))
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [])
    monkeypatch.setattr(run_store, "upsert_row_detail", lambda runs_dir, row: "boom")
    assert store_fill.main(a) == 2
    assert "store refused demo/demo: boom" in capsys.readouterr().out
