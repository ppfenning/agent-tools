from agent_tools import route_import
from agent_tools.cli import main

_TASK_PARTS = ("work", "demo", "1-build", "task.md")
_TASK_TEXT = "---\nstate: ready\ntitle: Do the task\n---\n\nDo the task\n"


def _row(**overrides):
    base = {
        "kind": "task", "initiative": "demo", "task_id": "task", "phase": "1-build",
        "state": "ready", "needs": [], "title": "Do the task", "surfaces": [], "body": "\nDo the task\n", "extra": {},
    }
    return {**base, **overrides}


def test_a_new_row_is_written():
    plan = route_import.plan_import([(_TASK_PARTS, _TASK_TEXT)], [])
    assert (list(plan.to_write), plan.unchanged, plan.skipped) == ([_row()], 0, 0)


def test_a_changed_row_is_written():
    stored = _row(state="todo")
    plan = route_import.plan_import([(_TASK_PARTS, _TASK_TEXT)], [stored])
    assert (list(plan.to_write), plan.unchanged, plan.skipped) == ([_row()], 0, 0)


def test_an_identical_row_is_skipped_as_unchanged():
    # a store row carries claim columns a file never does; they must not count against a match
    stored = _row(holder=None, epoch=None, expires_at=None)
    plan = route_import.plan_import([(_TASK_PARTS, _TASK_TEXT)], [stored])
    assert (list(plan.to_write), plan.unchanged, plan.skipped) == ([], 1, 0)


def test_initiative_md_is_skipped():
    plan = route_import.plan_import([(("work", "demo", "initiative.md"), "anything")], [])
    assert (list(plan.to_write), plan.unchanged, plan.skipped) == ([], 0, 1)


def test_a_malformed_file_is_skipped():
    plan = route_import.plan_import([(_TASK_PARTS, "no frontmatter here\n")], [])
    assert (list(plan.to_write), plan.unchanged, plan.skipped) == ([], 0, 1)


def test_files_sharing_a_store_key_import_the_first_and_skip_the_rest_on_every_run():
    # intake/x.md and intake/done/x.md both key to ("intake", "x"); the store keeps one row per key
    files = [(("intake", "x.md"), "---\ntitle: Root\n---\n"), (("intake", "done", "x.md"), "---\ntitle: Done\n---\n")]
    first = route_import.plan_import(files, [])
    second = route_import.plan_import(files, [{**first.to_write[0], "holder": None}])
    assert ([r["state"] for r in first.to_write], first.unchanged, first.skipped) == (["queued"], 0, 1)
    assert (list(second.to_write), second.unchanged, second.skipped) == ([], 1, 1)


def test_an_intake_file_named_by_an_initiatives_intake_field_imports_as_decomposed():
    files = [
        (("work", "demo", "initiative.md"), "---\nintake: intake/idea.md\n---\n\nAbout demo\n"),
        (("work", "demo", "1-build", "task.md"), _TASK_TEXT),
        (("intake", "idea.md"), "---\ntitle: An idea\n---\n\nBody\n"),
    ]
    plan = route_import.plan_import(files, [])
    intake_row = next(r for r in plan.to_write if r["kind"] == "intake")
    assert intake_row["state"] == "decomposed"


def test_an_intake_file_named_by_a_done_initiatives_intake_field_imports_as_landed():
    done_task_text = "---\nstate: done\ntitle: Do the task\n---\n\nDo the task\n"
    files = [
        (("work", "demo", "initiative.md"), "---\nintake: intake/idea.md\n---\n\nAbout demo\n"),
        (("work", "demo", "1-build", "task.md"), done_task_text),
        (("intake", "idea.md"), "---\ntitle: An idea\n---\n\nBody\n"),
    ]
    plan = route_import.plan_import(files, [])
    intake_row = next(r for r in plan.to_write if r["kind"] == "intake")
    assert intake_row["state"] == "landed"


def test_an_intake_file_named_by_no_initiative_still_imports_as_queued():
    files = [(("intake", "idea.md"), "---\ntitle: An idea\n---\n\nBody\n")]
    plan = route_import.plan_import(files, [])
    assert plan.to_write[0]["state"] == "queued"


def test_format_summary_reports_all_three_counts():
    plan = route_import.plan_import([(_TASK_PARTS, _TASK_TEXT)], [])
    assert route_import.format_summary(1, plan) == "written 1, unchanged 0, skipped 0"


def test_import_keeps_a_store_state_the_file_is_behind_on_the_ladder():
    # store: approved, file: ready (_TASK_TEXT) -- the file is behind, so import must not overwrite it.
    stored = _row(state="approved")
    plan = route_import.plan_import([(_TASK_PARTS, _TASK_TEXT)], [stored])
    assert (plan.to_write, plan.kept) == ((), (("task", "approved", "ready"),))
    assert route_import.format_summary(0, plan) == (
        "kept store state: task (store approved, file ready)\nwritten 0, unchanged 0, skipped 0"
    )


def test_import_still_writes_a_files_state_forward_over_the_stores():
    # store: ready, file: approved -- the file is ahead, so import writes it over the store as today.
    approved_text = "---\nstate: approved\ntitle: Do the task\n---\n\nDo the task\n"
    stored = _row(state="ready")
    plan = route_import.plan_import([(_TASK_PARTS, approved_text)], [stored])
    assert (list(plan.to_write), plan.kept) == ([_row(state="approved")], ())


def test_import_writes_a_done_tasks_other_field_change_even_though_state_is_unchanged():
    # store: done, file: done but a different title -- states are equal, so the edit must still land.
    done_text = "---\nstate: done\ntitle: Do the renamed task\n---\n\nDo the task\n"
    stored = _row(state="done")
    plan = route_import.plan_import([(_TASK_PARTS, done_text)], [stored])
    assert (list(plan.to_write), plan.kept) == ([_row(state="done", title="Do the renamed task")], ())


def _workspace(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake" / "done").mkdir(parents=True)
    (ws / "intake" / "idea.md").write_text("---\ntitle: An idea\n---\n\nBody\n")
    (ws / "intake" / "done" / "old.md").write_text("---\ntitle: Old idea\n---\n\nBody\n")
    (ws / "work" / "demo" / "1-build").mkdir(parents=True)
    (ws / "work" / "demo" / "1-build" / "task.md").write_text(_TASK_TEXT)
    (ws / "work" / "demo" / "initiative.md").write_text("---\ntitle: Demo\n---\n\nAbout demo\n")
    return ws


def _fake_store(monkeypatch):
    """A dict standing in for the store, wired to the names `_route_import` calls."""
    written: dict[tuple, dict] = {}
    monkeypatch.setattr("agent_tools.cli.run_store.read_queue", lambda *_a, **_k: list(written.values()))

    def upsert_detail(_runs_dir, row):
        written[(row["initiative"], row["task_id"])] = row  # run_store's upsert contract: keyed (initiative, task_id)
        return ""

    monkeypatch.setattr("agent_tools.cli.run_store.upsert_row_detail", upsert_detail)
    return written


def test_edge_second_run_writes_nothing(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    _fake_store(monkeypatch)

    rc1 = main(["route", "import", "--workspace", str(ws)])
    first = capsys.readouterr().out.strip()
    rc2 = main(["route", "import", "--workspace", str(ws)])
    second = capsys.readouterr().out.strip()

    assert (rc1, rc2) == (0, 0)
    assert first == "written 3, unchanged 0, skipped 1"  # skipped: work/demo/initiative.md, a row for no kind
    assert second == "written 0, unchanged 3, skipped 1"


def test_edge_derives_decomposed_state_from_the_real_walk(tmp_path, monkeypatch):
    # Proves `_route_import_files` itself reads `work/<id>/initiative.md`, not just a hand-built `files` list:
    # the 27-vs-3 mismatch this ticket fixes only closes if the real walker feeds that text to plan_import.
    ws = tmp_path / "workspace"
    (ws / "runs").mkdir(parents=True)
    (ws / "intake").mkdir()
    (ws / "intake" / "idea.md").write_text("---\ntitle: An idea\n---\n\nBody\n")
    (ws / "work" / "demo" / "1-build").mkdir(parents=True)
    (ws / "work" / "demo" / "1-build" / "task.md").write_text(_TASK_TEXT)  # state: ready, keeps demo not done
    (ws / "work" / "demo" / "initiative.md").write_text("---\nintake: intake/idea.md\n---\n\nAbout demo\n")
    written = _fake_store(monkeypatch)

    rc = main(["route", "import", "--workspace", str(ws)])

    assert rc == 0
    assert written[("intake", "idea")]["state"] == "decomposed"


def test_edge_without_workspace_falls_back_to_the_profile_workspace_dir(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"team: acme\nworkspace_dir: {ws}\n")
    _fake_store(monkeypatch)
    monkeypatch.chdir(tmp_path)  # cwd is not the workspace, so a "." default would find nothing

    rc = main(["route", "import", "--profile", str(profile)])

    assert (rc, capsys.readouterr().out.strip()) == (0, "written 3, unchanged 0, skipped 1")


def test_edge_refuses_a_workspace_with_no_work_or_intake_dir(tmp_path, monkeypatch, capsys):
    written = _fake_store(monkeypatch)

    rc = main(["route", "import", "--workspace", str(tmp_path)])

    assert rc == 2
    assert capsys.readouterr().out.startswith(f"route import: no work/ or intake/ under {tmp_path}")
    assert written == {}


def test_edge_exits_2_and_reports_the_partial_count_when_the_store_refuses(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    monkeypatch.setattr("agent_tools.cli.run_store.read_queue", lambda *_a, **_k: [])
    calls = []

    def upsert_detail(_runs_dir, row):
        calls.append(row)
        return "" if len(calls) == 1 else "no module named harness.store_queue"

    monkeypatch.setattr("agent_tools.cli.run_store.upsert_row_detail", upsert_detail)

    rc = main(["route", "import", "--workspace", str(ws)])
    out = capsys.readouterr().out.strip()

    assert rc == 2
    assert out.splitlines()[-1] == "written 1, unchanged 0, skipped 1"


def test_edge_prints_the_failing_rows_key_and_the_store_detail_before_the_summary(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    monkeypatch.setattr("agent_tools.cli.run_store.read_queue", lambda *_a, **_k: [])
    calls = []

    def upsert_detail(_runs_dir, row):
        calls.append(row)
        return "" if len(calls) == 1 else "No module named harness.store_queue"

    monkeypatch.setattr("agent_tools.cli.run_store.upsert_row_detail", upsert_detail)

    rc = main(["route", "import", "--workspace", str(ws)])
    lines = capsys.readouterr().out.strip().splitlines()
    failing = calls[1]

    key = f"{failing['initiative']}/{failing['task_id']}"
    assert rc == 2
    assert lines[0] == f"route import: store refused {key}: No module named harness.store_queue"
    assert lines[1] == "written 1, unchanged 0, skipped 1"
