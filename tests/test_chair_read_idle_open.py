from agent_tools.chair_read_idle_open import open_idle_stall, read_idle_open


def row(signature: str, ts: str, reason: str = "r", cause: str = "idle_stall") -> dict:
    return {"kind": "needs_chair", "cause": cause, "signature": signature, "reason": reason, "ts": ts}


def test_an_open_idle_stall_row_gives_its_signature_and_reason():
    rows = [row("blocked:a", "2026-10-06T01:00:00+00:00", "a waits on b")]
    assert open_idle_stall(rows, {"blocked:a"}) == ("blocked:a", "a waits on b")


def test_a_resolved_idle_stall_row_gives_nothing():
    assert open_idle_stall([row("blocked:a", "2026-10-06T01:00:00+00:00")], set()) == (None, None)


def test_a_needs_chair_row_with_another_cause_is_ignored():
    rows = [row("blocked:a", "2026-10-06T01:00:00+00:00", cause="stalled")]
    assert open_idle_stall(rows, {"blocked:a"}) == (None, None)


def test_with_two_open_idle_stall_rows_the_newest_wins():
    rows = [row("blocked:b", "2026-10-06T02:00:00+00:00", "new"), row("blocked:a", "2026-10-06T01:00:00+00:00", "old")]
    assert open_idle_stall(rows, {"blocked:a", "blocked:b"}) == ("blocked:b", "new")


def test_a_row_stored_in_action_json_is_read():
    stored = {"kind": "needs_chair", "ts": "2026-10-06T01:00:00+00:00", "action_json": '{"cause": "idle_stall", "signature": "s", "reason": "why"}'}
    assert open_idle_stall([stored], {"s"}) == ("s", "why")


def test_the_edge_with_no_store_gives_nothing(tmp_path):
    assert read_idle_open(tmp_path) == (None, None)
