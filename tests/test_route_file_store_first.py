import datetime
from pathlib import Path

import pytest

from agent_tools import route, run_store
from agent_tools.cli import main

NOW_RUNS_DIR = Path("/runs")
_TEXT = "---\nstate: todo\ntitle: T1\n---\nTask one.\n"


@pytest.fixture(autouse=True)
def _no_gh_sync_on_file(monkeypatch):
    """`route file` syncs each item it writes; nothing in this file may reach a real `gh`."""
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)


# -- route.write_filed_item, the unit itself --------------------------------------------------


def test_upsert_is_called_before_the_file_exists(tmp_path, monkeypatch):
    target = tmp_path / "work" / "alpha" / "p1" / "t1.md"
    seen = []

    def fake_upsert_row(runs_dir, row):
        seen.append(target.exists())
        return True

    monkeypatch.setattr(run_store, "upsert_row", fake_upsert_row)

    code = route.write_filed_item(NOW_RUNS_DIR, target, "task", ("alpha", "p1", "t1"), _TEXT)

    assert seen == [False]
    assert target.exists()
    assert code == 0


def test_a_failed_upsert_warns_and_still_writes_the_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "work" / "alpha" / "p1" / "t1.md"
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: False)

    code = route.write_filed_item(NOW_RUNS_DIR, target, "task", ("alpha", "p1", "t1"), _TEXT)

    assert code == 0
    assert target.read_text() == _TEXT
    out = capsys.readouterr().out
    assert str(target) in out
    assert "store unavailable" in out


def test_a_failed_file_write_after_a_good_upsert_exits_non_zero(tmp_path, monkeypatch, capsys):
    # A path whose parent is a file, not a directory, cannot be `mkdir`'d into and never accepts a write.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    target = blocker / "t1.md"
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)

    code = route.write_filed_item(NOW_RUNS_DIR, target, "task", ("alpha", "p1", "t1"), _TEXT)

    assert code != 0
    assert not target.exists()
    out = capsys.readouterr().out
    assert str(target) in out
    assert "store recorded" in out


def test_a_failed_file_write_after_a_failed_upsert_also_exits_non_zero(tmp_path, monkeypatch, capsys):
    """Neither side of a double failure gets to hide behind an uncaught exception: both are one printed
    line and a non-zero return, the same shape as the good-upsert failure above."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    target = blocker / "t1.md"
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: False)

    code = route.write_filed_item(NOW_RUNS_DIR, target, "task", ("alpha", "p1", "t1"), _TEXT)

    assert code != 0
    assert not target.exists()
    out = capsys.readouterr().out
    assert str(target) in out
    assert "store unavailable" in out


def test_an_unrecognized_shape_is_not_reported_as_a_store_outage(tmp_path, monkeypatch, capsys):
    """`queue_rows.parse_item` refuses a shape it does not know (here, two path parts for a `task` kind
    that wants three); the warning names the real reason, not a store that was never asked."""
    target = tmp_path / "work" / "alpha" / "t1.md"
    called = []
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: called.append(row) or True)

    code = route.write_filed_item(NOW_RUNS_DIR, target, "task", ("alpha", "t1"), _TEXT)

    assert code == 0
    assert called == []
    out = capsys.readouterr().out
    assert "unrecognized item shape" in out


# -- `cox route file`, the command the ticket names -------------------------------------------


def _write_file_profile(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\n"
        "provider_profile: /opt/providers/acme.yaml\n"
    )
    return profile, ws


def test_cox_route_file_upserts_the_task_row_before_the_task_file_exists(tmp_path, monkeypatch):
    profile, ws = _write_file_profile(tmp_path)
    slug = route.slugify("Fix the thing")
    task_path = ws / "work" / slug / "build" / f"{slug}.md"
    initiative_path = ws / "work" / slug / "initiative.md"
    calls = []

    def fake_upsert_row(runs_dir, row):
        calls.append((row["kind"], task_path.exists()))
        return True

    monkeypatch.setattr(run_store, "upsert_row", fake_upsert_row)

    rc = main(["route", "file", "--profile", str(profile), "--repo", "/repos/widget", "--title", "Fix the thing"])

    assert rc == 0
    # Exactly one upsert: the task carries a queue row, the initiative doc beside it does not.
    assert calls == [("task", False)]
    assert task_path.exists()
    assert initiative_path.exists()


def test_cox_route_file_intake_upserts_the_intake_row_before_the_intake_file_exists(tmp_path, monkeypatch):
    profile, ws = _write_file_profile(tmp_path)
    expected_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    slug = route.slugify("Fix the thing")
    intake_path = ws / "intake" / f"{expected_date}-{slug}.md"
    calls = []

    def fake_upsert_row(runs_dir, row):
        calls.append((row["kind"], intake_path.exists()))
        return True

    monkeypatch.setattr(run_store, "upsert_row", fake_upsert_row)

    rc = main([
        "route", "file", "--profile", str(profile), "--repo", "/repos/widget",
        "--title", "Fix the thing", "--intake",
    ])

    assert rc == 0
    assert calls == [("intake", False)]
    assert intake_path.exists()


def test_cox_route_file_still_files_the_task_when_the_store_is_unavailable(tmp_path, monkeypatch, capsys):
    profile, ws = _write_file_profile(tmp_path)
    slug = route.slugify("Fix the thing")
    task_path = ws / "work" / slug / "build" / f"{slug}.md"
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: False)

    rc = main(["route", "file", "--profile", str(profile), "--repo", "/repos/widget", "--title", "Fix the thing"])

    assert rc == 0
    assert task_path.exists()
    assert "store unavailable" in capsys.readouterr().out
