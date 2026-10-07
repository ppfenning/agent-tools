from agent_tools.chair_read_handoff_stops import handoff_stops_from, read_handoff_stops
from agent_tools.chair_read_quarantined import body_sha

BODY = "Do the thing.\n"


def _item(**over):
    return {"initiative": "i", "task_id": "a", "state": "ready", "body": BODY, "surfaces": ["a.py"], "attempts": [], **over}


def test_a_ready_ticket_with_two_attempts_on_its_body_gives_one_row_with_the_newest_reason():
    attempts = [
        {"ts": "2026-10-01T00:00:00Z", "reason": "old reason", "body_sha": body_sha(BODY)},
        {"ts": "2026-10-02T00:00:00Z", "reason": "new reason", "body_sha": body_sha(BODY)},
    ]
    assert handoff_stops_from([_item(attempts=attempts)]) == [
        {"initiative": "i", "task_id": "a", "state": "ready", "reason": "new reason", "surfaces": ["a.py"], "widenings": 0}
    ]


def test_a_ticket_whose_only_attempt_is_on_an_older_body_gives_no_row():
    attempts = [{"ts": "2026-10-01T00:00:00Z", "reason": "r", "body_sha": body_sha("an older body")}]
    assert handoff_stops_from([_item(attempts=attempts)]) == []


def test_a_done_ticket_gives_no_row():
    assert handoff_stops_from([_item(state="done", attempts=[{"ts": "t", "reason": "r"}])]) == []


def test_a_body_with_one_widening_note_gives_widenings_1():
    body = BODY + "\nWidened by the chair 2026-10-01: b.py is now in surfaces for a rename only.\n"
    rows = handoff_stops_from([_item(body=body, attempts=[{"ts": "t", "reason": "r"}])])
    assert rows[0]["widenings"] == 1


def test_surfaces_keep_a_new_suffix_as_written():
    rows = handoff_stops_from([_item(surfaces=["a.py", "b.py (new)"], attempts=[{"ts": "t", "reason": "r"}])])
    assert rows[0]["surfaces"] == ["a.py", "b.py (new)"]


def test_the_edge_skips_a_malformed_ticket_and_does_not_raise(tmp_path):
    phase = tmp_path / "work" / "i" / "p1"
    phase.mkdir(parents=True)
    (phase / "a.md").write_text("---\nstate: ready\nsurfaces: [a.py]\nattempts:\n  - {ts: t1, reason: why}\n---\nBody\n")
    (phase / "b.md").write_text("---\n: : [\n---\nBody\n")
    assert [r["task_id"] for r in read_handoff_stops(tmp_path, "file")] == ["a"]
