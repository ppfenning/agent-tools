import os

from agent_tools import chair_read_intake as cri
from agent_tools import route


def _groups(entries, initiatives=()):
    return route.intake_groups(entries, list(initiatives))


def _entry(path, initiative=None, done=False):
    return {"id": path, "title": path, "initiative": initiative, "done": done, "path": path}


def _intake_row(task_id, initiative=None, state="queued"):
    return {
        "kind": "intake",
        "initiative": "intake",
        "task_id": task_id,
        "phase": "",
        "state": state,
        "needs": [],
        "title": task_id,
        "surfaces": [],
        "body": "",
        "extra": {"initiative": initiative} if initiative is not None else {},
    }


def _task_row(task_id, state, initiative):
    return {
        "kind": "task",
        "initiative": initiative,
        "task_id": task_id,
        "phase": "p1",
        "state": state,
        "needs": [],
        "title": task_id,
        "surfaces": [],
        "body": "",
        "extra": {},
    }


def test_queued_files_sort_oldest_first():
    groups = _groups([_entry("intake/new.md"), _entry("intake/old.md")])
    mtimes = {"intake/new.md": 200.0, "intake/old.md": 100.0}
    assert cri.queued_oldest_first(groups, mtimes) == ["intake/old.md", "intake/new.md"]


def test_equal_mtimes_fall_back_to_path():
    groups = _groups([_entry("intake/b.md"), _entry("intake/a.md")])
    assert cri.queued_oldest_first(groups, {"intake/a.md": 5.0, "intake/b.md": 5.0}) == ["intake/a.md", "intake/b.md"]


def test_a_root_file_whose_initiative_is_done_is_not_returned():
    groups = _groups([_entry("intake/a.md", initiative="x")], [{"id": "x", "done": True, "text": ""}])
    assert cri.queued_oldest_first(groups, {}) == []


def test_a_root_file_with_no_initiative_is_returned():
    groups = _groups([_entry("intake/a.md")])
    assert cri.queued_oldest_first(groups, {"intake/a.md": 1.0}) == ["intake/a.md"]


def test_a_file_under_done_is_not_returned():
    groups = _groups([_entry("intake/a.md", done=True)])
    assert cri.queued_oldest_first(groups, {}) == []


def test_a_profile_with_one_source_is_configured():
    assert cri.has_sources(["github"]) is True


def test_a_profile_with_no_sources_is_not_configured():
    assert cri.has_sources([]) is False


def test_the_edge_lists_intake_by_mtime_and_skips_done(tmp_path):
    (tmp_path / "intake" / "done").mkdir(parents=True)
    for rel, mtime in (("new.md", 200), ("old.md", 100), ("done/gone.md", 50)):
        p = tmp_path / "intake" / rel
        p.write_text("body\n", encoding="utf-8")
        os.utime(p, (mtime, mtime))
    assert cri.read_intake(tmp_path) == ["intake/old.md", "intake/new.md"]
    assert cri.read_intake(tmp_path / "missing") == []


def test_the_edge_drops_a_root_file_whose_initiative_is_done(tmp_path):
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text("---\ninitiative: x\n---\nbody\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "x" / "initiative.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1" / "t.md").write_text("---\nstate: done\n---\n", encoding="utf-8")
    assert cri.read_intake(tmp_path) == []
    (tmp_path / "intake" / "b.md").write_text("body\n", encoding="utf-8")
    assert cri.read_intake(tmp_path) == ["intake/b.md"]


def test_a_queued_row_with_no_initiative_is_returned():
    rows = [_intake_row("a")]
    assert cri.intake_from_rows(rows) == rows


def test_a_queued_row_whose_initiative_has_an_undone_task_row_is_withheld():
    rows = [_intake_row("a", initiative="x"), _task_row("t1", "todo", "x")]
    assert cri.intake_from_rows(rows) == []


def test_a_queued_row_whose_initiative_items_are_all_done_is_withheld():
    rows = [_intake_row("a", initiative="x"), _task_row("t1", "done", "x")]
    assert cri.intake_from_rows(rows) == []


def test_a_non_queued_intake_row_is_never_returned():
    rows = [_intake_row("a", state="done")]
    assert cri.intake_from_rows(rows) == []


def _stems(paths):
    return {p.rsplit("/", 1)[-1].removesuffix(".md") for p in paths}


def test_parity_the_same_board_as_rows_and_as_files_agrees(tmp_path):
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "a.md").write_text("---\ninitiative: x\n---\nbody\n", encoding="utf-8")
    (tmp_path / "intake" / "b.md").write_text("body\n", encoding="utf-8")
    (tmp_path / "work" / "x" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "x" / "initiative.md").write_text("# x\n", encoding="utf-8")
    task = tmp_path / "work" / "x" / "p1" / "t.md"
    for state in ("todo", "done"):
        task.write_text(f"---\nstate: {state}\n---\n", encoding="utf-8")
        rows = [_intake_row("a", initiative="x"), _intake_row("b"), _task_row("t", state, "x")]
        assert {r["task_id"] for r in cri.intake_from_rows(rows)} == _stems(cri.read_intake(tmp_path)) == {"b"}


def test_the_edge_reads_sources_from_the_profile(tmp_path):
    profile = tmp_path / "profile.yaml"
    assert cri.read_sources_configured(profile) is False
    profile.write_text('sources: {"github": {"repos": ["a/b"]}}\n', encoding="utf-8")
    assert cri.read_sources_configured(profile) is True
    profile.write_text("sources: {}\n", encoding="utf-8")
    assert cri.read_sources_configured(profile) is False
