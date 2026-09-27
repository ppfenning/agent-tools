from pathlib import Path

import pytest

from agent_tools import chair, queue_rows, route, run_store

NOW = "2026-09-27T00:00:00Z"

_T0 = "---\nstate: done\ntitle: T0\n---\nTask zero.\n"
_T1 = "---\nstate: ready\nneeds: [t0]\ntitle: T1\n---\nTask one.\n"
_T2 = "---\nstate: todo\ntitle: T2\n---\nTask two.\n"
_B0 = "---\nstate: approved\ntitle: B0\n---\nTask b0.\n"
_IDEA = "---\ntitle: Idea\n---\nAn idea.\n"
_OLD = "---\ntitle: Old\n---\nDone already.\n"
_LINKED = "---\ntitle: Linked\ninitiative: alpha\n---\nLinked to alpha.\n"


def _write(root: Path, relpath: str, text: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_board(root: Path) -> None:
    _write(root, "work/alpha/p1/t0.md", _T0)
    _write(root, "work/alpha/p1/t1.md", _T1)
    _write(root, "work/alpha/p1/t2.md", _T2)
    _write(root, "work/beta/p1/b0.md", _B0)
    _write(root, "intake/idea.md", _IDEA)
    _write(root, "intake/done/old.md", _OLD)
    _write(root, "intake/linked.md", _LINKED)


def _file_items(root: Path) -> list:
    return [
        route.work_item(route.parse_frontmatter(text)[0], initiative=initiative, phase_dir=phase, stem=stem)
        for initiative, phase, stem, text in [
            ("alpha", "p1", "t0", _T0), ("alpha", "p1", "t1", _T1), ("alpha", "p1", "t2", _T2), ("beta", "p1", "b0", _B0),
        ]
    ]


def _file_intake_groups(items: list) -> dict:
    files = {"done/old.md": _OLD, "idea.md": _IDEA, "linked.md": _LINKED}
    ids = sorted({item["initiative"] for item in items})
    initiatives = [{"id": iid, "done": done, "text": ""} for iid, done in route.initiative_states(ids, items).items()]
    return route.intake_groups(route.intake_entries(files), initiatives)


def _queue_row(kind: str, path_parts: tuple, text: str, *, holder=None, epoch=0, expires_at=None) -> dict:
    """One `run_store.read_queue` row: `queue_rows.parse_item` plus the claim columns `_queue_rows_from` always pads in."""
    row = queue_rows.parse_item(kind, path_parts, text)
    return {**row, "holder": holder, "epoch": epoch, "expires_at": expires_at}


def _board_rows() -> list:
    return [
        _queue_row("task", ("alpha", "p1", "t0"), _T0),
        _queue_row("task", ("alpha", "p1", "t1"), _T1),
        _queue_row("task", ("alpha", "p1", "t2"), _T2),
        _queue_row("task", ("beta", "p1", "b0"), _B0),
        _queue_row("intake", ("idea",), _IDEA),
        _queue_row("intake", ("done", "old"), _OLD),
        _queue_row("intake", ("linked",), _LINKED),
    ]


def test_context_from_rows_matches_the_same_board_read_from_files(tmp_path):
    _write_board(tmp_path)
    items = _file_items(tmp_path)
    expected = (_file_intake_groups(items), route.initiative_summaries(items))

    assert route.context_from_rows(_board_rows(), NOW) == expected


def test_context_rows_falls_back_when_the_store_is_unavailable(monkeypatch):
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: [])

    assert route.context_rows(Path("/does/not/matter"), NOW) is None


def test_context_rows_reads_through_read_queue_when_the_store_answers(monkeypatch):
    rows = _board_rows()
    monkeypatch.setattr(run_store, "read_queue", lambda runs_dir: rows)

    assert route.context_rows(Path("/does/not/matter"), NOW) == route.context_from_rows(rows, NOW)


def test_launch_claim_defaults_ttl_to_the_chairs_own_lease_ttl(monkeypatch):
    captured = {}

    def fake_claim_outcome(runs_dir, initiative, task_id, holder, ttl_s, now):
        captured["ttl_s"] = ttl_s
        return {"holder": holder, "epoch": 1, "expires_at": "later"}, None

    monkeypatch.setattr(run_store, "claim_outcome", fake_claim_outcome)

    route.launch_claim(Path("/runs"), "alpha", "t1", "holder-1", NOW)

    assert captured["ttl_s"] == chair.DEFAULT_LEASE_TTL_SECONDS


def test_launch_claim_names_the_holder_when_another_holder_has_it(monkeypatch):
    monkeypatch.setattr(run_store, "claim_outcome", lambda *a, **k: (None, run_store.HELD))
    monkeypatch.setattr(
        run_store, "read_queue",
        lambda runs_dir, initiative, kind=None: [_queue_row("task", ("alpha", "p1", "t1"), _T1, holder="lane-2@host:9")],
    )

    claim, blocker = route.launch_claim(Path("/runs"), "alpha", "t1", "holder-1", NOW)

    assert claim is None
    assert blocker == "lane-2@host:9"


def test_launch_claim_gate_refuses_a_held_task_with_the_launch_refusal_code():
    code, lines = route.launch_claim_gate(None, "lane-2@host:9")

    assert code == 2
    assert len(lines) == 1
    assert "lane-2@host:9" in lines[0]


def test_launch_claim_gate_proceeds_with_one_warning_when_the_store_is_unavailable():
    code, lines = route.launch_claim_gate(None, run_store.UNAVAILABLE)

    assert code is None
    assert len(lines) == 1


def test_launch_claim_gate_is_silent_when_claimed():
    assert route.launch_claim_gate({"holder": "holder-1", "epoch": 1, "expires_at": "later"}, None) == (None, [])


def test_run_under_claim_refuses_a_held_task_and_never_runs_the_body(monkeypatch):
    monkeypatch.setattr(run_store, "claim_outcome", lambda *a, **k: (None, run_store.HELD))
    monkeypatch.setattr(
        run_store, "read_queue",
        lambda runs_dir, initiative, kind=None: [_queue_row("task", ("alpha", "p1", "t1"), _T1, holder="lane-2@host:9")],
    )

    def body():
        raise AssertionError("body must not run when the claim is refused")

    code, lines, value = route.run_under_claim(Path("/runs"), "alpha", "t1", "holder-1", NOW, body)

    assert code == 2
    assert value is None
    assert "lane-2@host:9" in lines[0]


def test_run_under_claim_runs_the_body_once_a_free_task_is_claimed(monkeypatch):
    claim = {"holder": "holder-1", "epoch": 1, "expires_at": "later"}
    monkeypatch.setattr(run_store, "claim_outcome", lambda *a, **k: (claim, None))
    released = []
    monkeypatch.setattr(run_store, "release_row", lambda *a, **k: released.append((a, k)))

    code, lines, value = route.run_under_claim(Path("/runs"), "alpha", "t1", "holder-1", NOW, lambda: "launched")

    assert (code, lines, value) == (None, [], "launched")
    assert released == []


def test_run_under_claim_proceeds_with_one_warning_when_the_store_is_unavailable(monkeypatch):
    monkeypatch.setattr(run_store, "claim_outcome", lambda *a, **k: (None, run_store.UNAVAILABLE))

    code, lines, value = route.run_under_claim(Path("/runs"), "alpha", "t1", "holder-1", NOW, lambda: "launched")

    assert code is None
    assert len(lines) == 1
    assert value == "launched"


def test_run_under_claim_releases_the_claim_on_a_failure_before_the_run_starts(monkeypatch):
    claim = {"holder": "holder-1", "epoch": 1, "expires_at": "later"}
    monkeypatch.setattr(run_store, "claim_outcome", lambda *a, **k: (claim, None))
    released = []
    monkeypatch.setattr(run_store, "release_row", lambda runs_dir, initiative, task_id, holder: released.append((runs_dir, initiative, task_id, holder)))
    runs_dir = Path("/runs")

    def body():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        route.run_under_claim(runs_dir, "alpha", "t1", "holder-1", NOW, body)

    assert released == [(runs_dir, "alpha", "t1", "holder-1")]
