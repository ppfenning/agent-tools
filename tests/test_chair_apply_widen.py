from pathlib import Path

from agent_tools.chair_apply_widen import apply_widen

ACTION = {
    "kind": "widen_ticket", "initiative": "i", "task_id": "t1",
    "paths": ["src/app.rs"], "additions": ["accessor"], "reason": "handoff", "epoch": 1,
}


def _ticket(tmp_path: Path, state: str, extra: str = "") -> Path:
    path = tmp_path / "work" / "i" / "p1" / "t1.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nid: t1\nstate: {state}\nsurfaces: [agent_tools/a.py]\n{extra}---\nBody.\n", encoding="utf-8")
    return path


def test_a_ready_ticket_gains_the_path_and_a_note_and_stays_ready(tmp_path) -> None:
    path = _ticket(tmp_path, "ready")
    ok, reason = apply_widen(tmp_path, ACTION, "2026-10-07")
    text = path.read_text(encoding="utf-8")
    assert ok is True
    assert reason == "widened t1: src/app.rs"
    assert "surfaces: [agent_tools/a.py, src/app.rs]" in text
    assert "state: ready\n" in text
    assert "Widened by the chair" in text and "accessor" in text


def test_a_quarantined_looking_ticket_keeps_its_attempts_and_is_ready(tmp_path) -> None:
    path = _ticket(tmp_path, "blocked", "attempts: 3\n")
    ok, _ = apply_widen(tmp_path, ACTION, "2026-10-07")
    text = path.read_text(encoding="utf-8")
    assert ok is True
    assert "attempts: 3\n" in text
    assert "state: ready\n" in text


def test_a_missing_ticket_returns_false_without_raising(tmp_path) -> None:
    assert apply_widen(tmp_path, ACTION, "2026-10-07") == (False, "ticket not found")


def test_a_done_ticket_returns_false_and_is_not_rewritten(tmp_path) -> None:
    path = _ticket(tmp_path, "done")
    before = path.read_text(encoding="utf-8")
    ok, reason = apply_widen(tmp_path, ACTION, "2026-10-07")
    assert ok is False
    assert "done" in reason
    assert path.read_text(encoding="utf-8") == before
