import argparse
import json

import pytest

from agent_tools.courier import (
    Reference,
    ack,
    ack_file,
    append_line,
    entries,
    format_reference,
    inbox,
    parse_reference,
    resolve,
    send,
)

_REFS = [
    Reference("run", "r1"),
    Reference("task", "work/init/p1/t1.md"),
    Reference("pr", "r1:draft_pr_create"),
    Reference("intake", "abc"),
    Reference("proposal", "r1:item_create"),
    Reference("finding", "r1"),
]


@pytest.mark.parametrize("ref", _REFS, ids=lambda r: r.kind)
def test_round_trip(ref):
    assert parse_reference(format_reference(ref)) == ref


def test_unknown_kind_returns_none():
    assert parse_reference("coxswain://widget/r1") is None


def test_malformed_reference_returns_none():
    assert parse_reference("not-a-reference") is None


def _run_record_fixture(tmp_path):
    task = tmp_path / "runs" / "r1" / "tasks" / "p1" / "t1.json"
    task.parent.mkdir(parents=True)
    record = {"run": "r1", "arbitration": {"reasoning": "the fix broke the build"}, "adversary": []}
    task.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_resolve_run(tmp_path):
    record = _run_record_fixture(tmp_path)
    assert resolve(Reference("run", "r1"), tmp_path) == record


def test_resolve_finding(tmp_path):
    _run_record_fixture(tmp_path)
    assert resolve(Reference("finding", "r1"), tmp_path) == {"reasoning": "the fix broke the build"}


def test_resolve_finding_falls_back_to_adversary_why_wrong(tmp_path):
    task = tmp_path / "runs" / "r2" / "tasks" / "p1" / "t1.json"
    task.parent.mkdir(parents=True)
    task.write_text(json.dumps({"adversary": [{"why_wrong": "missed the edge case"}]}), encoding="utf-8")
    assert resolve(Reference("finding", "r2"), tmp_path) == {"reasoning": "missed the edge case"}


def _ledger_fixture(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"key": "r1:draft_pr_create", "kind": "draft_pr_create", "run_id": "r1"},
        {"key": "r1:item_create", "kind": "item_create", "run_id": "r1"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return ledger, rows


def test_resolve_pr(tmp_path):
    ledger, rows = _ledger_fixture(tmp_path)
    assert resolve(Reference("pr", "r1:draft_pr_create"), tmp_path, ledger_path=ledger) == rows[0]


def test_resolve_proposal(tmp_path):
    ledger, rows = _ledger_fixture(tmp_path)
    assert resolve(Reference("proposal", "r1:item_create"), tmp_path, ledger_path=ledger) == rows[1]


def _work_item_fixture(tmp_path, rel_path):
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: "T1"\nstatus: "open"\n---\nBody text.\n', encoding="utf-8")
    return path


def test_resolve_task(tmp_path):
    _work_item_fixture(tmp_path, "work/init/p1/t1.md")
    assert resolve(Reference("task", "work/init/p1/t1.md"), tmp_path) == {
        "id": "T1", "status": "open", "body": "Body text.",
    }


def test_resolve_task_by_bare_id_in_a_two_level_work_tree(tmp_path):
    _work_item_fixture(tmp_path, "work/init/p1/T1.md")
    assert resolve(Reference("task", "T1"), tmp_path) == {
        "id": "T1", "status": "open", "body": "Body text.", "path": "work/init/p1/T1.md",
    }


def test_resolve_initiative_by_slug(tmp_path):
    path = tmp_path / "work" / "myinit" / "initiative.md"
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: "myinit"\ntitle: "My Initiative"\n---\nBody text.\n', encoding="utf-8")
    assert resolve(Reference("initiative", "myinit"), tmp_path) == {
        "id": "myinit", "title": "My Initiative", "body": "Body text.", "path": "work/myinit/initiative.md",
    }


def test_resolve_unknown_task_id_is_none(tmp_path):
    _work_item_fixture(tmp_path, "work/init/p1/t1.md")
    assert resolve(Reference("task", "nope"), tmp_path) is None


def test_resolve_unknown_initiative_id_is_none(tmp_path):
    path = tmp_path / "work" / "myinit" / "initiative.md"
    path.parent.mkdir(parents=True)
    path.write_text('---\nid: "myinit"\n---\nBody text.\n', encoding="utf-8")
    assert resolve(Reference("initiative", "nope"), tmp_path) is None


def test_resolve_intake(tmp_path):
    """The ticket id comes from frontmatter, not the filename stem, so this fixture's filename
    deliberately differs from the `id` it resolves by."""
    intake = tmp_path / "intake" / "2026-09-16-foo.md"
    intake.parent.mkdir(parents=True)
    intake.write_text('---\nid: "abc"\ntitle: "Foo"\n---\nBody text.\n', encoding="utf-8")
    assert resolve(Reference("intake", "abc"), tmp_path) == {
        "id": "abc", "title": "Foo", "body": "Body text.",
    }


def test_resolve_missing_run_is_none(tmp_path):
    assert resolve(Reference("run", "nope"), tmp_path) is None


def test_send_builds_an_unacknowledged_entry():
    entry = send(Reference("run", "r1"), "chair-2026-09-16", "cos", "look at this", "m1")
    assert entry == {
        "ref": "coxswain://run/r1", "from": "chair-2026-09-16", "to": "cos", "note": "look at this",
        "id": "m1", "ack": False,
    }


def test_append_line_on_an_empty_blob_writes_one_line():
    entry = send(Reference("run", "r1"), "a", "b", "note", "m1")
    blob = append_line("", entry)
    lines = blob.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == entry


def test_append_line_on_a_non_empty_blob_keeps_the_prior_lines_and_adds_one():
    first = append_line("", send(Reference("run", "r1"), "a", "b", "note", "m1"))
    entry2 = send(Reference("run", "r2"), "a", "b", "note two", "m2")
    blob = append_line(first, entry2)
    lines = blob.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1]) == entry2


def _bus(entries):
    blob = ""
    for entry in entries:
        blob = append_line(blob, entry)
    return blob


def test_inbox_returns_only_unacknowledged_entries():
    e1 = send(Reference("run", "r1"), "a", "cos", "one", "m1")
    e2 = send(Reference("run", "r2"), "a", "cos", "two", "m2")
    blob = _bus([e1, {**e2, "ack": True}])
    assert inbox(blob) == [e1]


def test_inbox_filters_by_label():
    e1 = send(Reference("run", "r1"), "a", "cos", "one", "m1")
    e2 = send(Reference("run", "r2"), "a", "epic", "two", "m2")
    blob = _bus([e1, e2])
    assert inbox(blob, label="epic") == [e2]


def test_inbox_routes_the_generic_chair_to_the_labels_that_hold_or_start_with_it():
    e1 = send(Reference("run", "r1"), "a", "chair", "one", "m1")
    blob = _bus([e1])
    assert inbox(blob, label="chair-0.6.0-G", holder="chair-0.6.0-G") == [e1]
    assert inbox(blob, label="chair-0.7.0-H", holder="chair-0.6.0-G") == [e1]


def test_inbox_does_not_route_a_specific_chair_label_to_a_different_one():
    e1 = send(Reference("run", "r1"), "a", "chair-0.5.0-F", "one", "m1")
    blob = _bus([e1])
    assert inbox(blob, label="chair-0.6.0-G", holder="chair-0.6.0-G") == []


def test_inbox_still_requires_an_exact_match_for_a_non_chair_label():
    e1 = send(Reference("run", "r1"), "a", "cos", "one", "m1")
    blob = _bus([e1])
    assert inbox(blob, label="epic", holder="chair-0.6.0-G") == []
    assert inbox(blob, label="cos", holder="chair-0.6.0-G") == [e1]


def test_ack_flips_one_entrys_flag_and_leaves_the_others_unchanged():
    e1 = send(Reference("run", "r1"), "a", "cos", "one", "m1")
    e2 = send(Reference("run", "r2"), "a", "cos", "two", "m2")
    blob = _bus([e1, e2])
    updated = ack(blob, "m1")
    assert inbox(updated) == [e2]


def test_ack_of_an_unknown_id_leaves_the_blob_unchanged():
    blob = _bus([send(Reference("run", "r1"), "a", "cos", "one", "m1")])
    assert ack(blob, "nope") == blob


def test_two_acks_of_different_ids_both_survive(tmp_path):
    path = tmp_path / "courier.jsonl"
    path.write_text(
        _bus([send(Reference("run", "r1"), "a", "cos", "one", "m1"), send(Reference("run", "r2"), "a", "cos", "two", "m2")]),
        encoding="utf-8",
    )
    assert ack_file(path, "m1") and ack_file(path, "m2")
    assert [e["ack"] for e in entries(path.read_text(encoding="utf-8"))] == [True, True]


def test_ack_file_of_an_unknown_id_writes_nothing(tmp_path):
    path = tmp_path / "courier.jsonl"
    text = _bus([send(Reference("run", "r1"), "a", "cos", "one", "m1")])
    path.write_text(text, encoding="utf-8")
    assert not ack_file(path, "nope")
    assert path.read_text(encoding="utf-8") == text


def test_a_torn_line_is_skipped_by_the_reader():
    good = send(Reference("run", "r1"), "a", "cos", "one", "m1")
    blob = _bus([good]) + '{"ref": "coxswain://run/a", "fr\n42\n'
    assert entries(blob) == [good]
    assert inbox(blob) == [good]


def test_ack_after_a_torn_final_line_still_lands(tmp_path):
    path = tmp_path / "courier.jsonl"
    path.write_text(_bus([send(Reference("run", "r1"), "a", "cos", "one", "m1")]) + '{"ref": "co', encoding="utf-8")
    assert ack_file(path, "m1")
    assert [e["ack"] for e in entries(path.read_text(encoding="utf-8"))] == [True]


def _profile(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {ws}\n")
    return profile, ws


def test_courier_send_refuses_a_reference_that_fails_to_parse(tmp_path):
    from agent_tools.cli import _courier_send

    profile, ws = _profile(tmp_path)
    a = argparse.Namespace(profile=str(profile), ref="not-a-reference", to="cos", note="hi")
    assert _courier_send(a) != 0
    assert not (ws / "courier.jsonl").exists()


def test_courier_send_refuses_a_reference_that_fails_to_resolve(tmp_path):
    from agent_tools.cli import _courier_send

    profile, ws = _profile(tmp_path)
    a = argparse.Namespace(profile=str(profile), ref="coxswain://run/nope", to="cos", note="hi")
    assert _courier_send(a) != 0
    assert not (ws / "courier.jsonl").exists()


def test_courier_inbox_degrades_to_no_holder_rather_than_raising_on_a_corrupt_lock_file(tmp_path, capsys):
    from agent_tools import chair
    from agent_tools.cli import _courier_inbox

    profile, ws = _profile(tmp_path)
    (ws / "courier.jsonl").write_text(_bus([send(Reference("run", "r1"), "a", "chair", "for chair", "m1")]), encoding="utf-8")
    runs_dir = ws / "runs"
    runs_dir.mkdir()
    chair.chair_path(runs_dir).write_text("not json", encoding="utf-8")
    rc = _courier_inbox(argparse.Namespace(profile=str(profile), label="chair-0.6.0-G"))
    assert rc == 0
    assert "for chair" in capsys.readouterr().out


def test_courier_inbox_for_a_non_chair_label_is_unaffected_by_a_corrupt_lock_file(tmp_path, capsys):
    from agent_tools import chair
    from agent_tools.cli import _courier_inbox

    profile, ws = _profile(tmp_path)
    (ws / "courier.jsonl").write_text(_bus([send(Reference("run", "r1"), "a", "cos", "for cos", "m1")]), encoding="utf-8")
    runs_dir = ws / "runs"
    runs_dir.mkdir()
    chair.chair_path(runs_dir).write_text("not json", encoding="utf-8")
    rc = _courier_inbox(argparse.Namespace(profile=str(profile), label="cos"))
    assert rc == 0
    assert "for cos" in capsys.readouterr().out


def test_courier_inbox_prints_only_the_labels_unacknowledged_entries(tmp_path, capsys):
    from agent_tools.cli import _courier_inbox

    profile, ws = _profile(tmp_path)
    mine = send(Reference("run", "r1"), "a", "cos", "for cos", "m1")
    theirs = send(Reference("run", "r2"), "a", "epic", "for epic", "m2")
    (ws / "courier.jsonl").write_text(_bus([mine, theirs]), encoding="utf-8")
    rc = _courier_inbox(argparse.Namespace(profile=str(profile), label="cos"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "for cos" in out and "for epic" not in out


def test_courier_ack_flips_the_entry_and_a_second_ack_reports_no_entry(tmp_path, capsys):
    from agent_tools.cli import _courier_ack, _courier_inbox

    profile, ws = _profile(tmp_path)
    entry = send(Reference("run", "r1"), "a", "cos", "for cos", "m1")
    (ws / "courier.jsonl").write_text(_bus([entry]), encoding="utf-8")
    assert _courier_ack(argparse.Namespace(profile=str(profile), id="m1")) == 0
    _courier_inbox(argparse.Namespace(profile=str(profile), label=None))
    assert "for cos" not in capsys.readouterr().out
    assert _courier_ack(argparse.Namespace(profile=str(profile), id="nope")) != 0


class _FakeBeater:
    pid = 424242


def test_bare_cox_prints_the_inbox_for_the_chair_label_it_just_took(tmp_path, monkeypatch, capsys):
    """End to end through `main`: chair take succeeds, the inbox line for that
    chair's own label prints before the (stubbed) exec — `tests/test_launcher.py`'s
    own `_fake_spawn`/`shutil.which` stubs, reused so the beater and claude are
    never really spawned."""
    import datetime

    from agent_tools.cli import main

    profile, ws = _profile(tmp_path)
    label = f"chair-{datetime.datetime.now(datetime.UTC):%Y-%m-%d}"
    entry = send(Reference("run", "r1"), "a", label, "look at this", "m1")
    (ws / "courier.jsonl").write_text(append_line("", entry), encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr("agent_tools.cli._spawn", lambda argv: _FakeBeater())
    monkeypatch.setattr("os.execvp", lambda *a: None)
    monkeypatch.setattr("os.chdir", lambda *a: None)
    rc = main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "look at this" in out
