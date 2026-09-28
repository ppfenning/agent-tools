import json

from agent_tools.chair_login_watch import (
    due_for_login_check,
    login_blocked,
    login_needs_chair,
    merge_login_versions,
)

NOW = "2026-09-27T12:00:00Z"


def _host(name: str, state: str = "active", versions_json: dict | None = None) -> dict:
    return {"name": name, "state": state, "versions_json": versions_json or {}}


def test_a_host_checked_thirty_one_minutes_ago_is_due():
    host = _host("h", versions_json={"login_checked_at": "2026-09-27T11:29:00Z"})
    assert due_for_login_check([host], NOW) == ["h"]


def test_a_host_checked_ten_minutes_ago_is_not_due():
    host = _host("h", versions_json={"login_checked_at": "2026-09-27T11:50:00Z"})
    assert due_for_login_check([host], NOW) == []


def test_a_draining_host_past_threshold_is_not_due():
    host = _host("h", state="draining", versions_json={"login_checked_at": "2026-09-27T11:29:00Z"})
    assert due_for_login_check([host], NOW) == []


def test_a_host_that_has_never_been_checked_is_due():
    host = _host("h", versions_json={})
    assert due_for_login_check([host], NOW) == ["h"]


def test_a_host_with_login_ok_false_is_blocked_with_one_needs_chair_action():
    host = _host("h", versions_json={"login_ok": False})
    assert login_blocked([host]) == {"h"}
    assert login_needs_chair([host]) == [{"kind": "needs_chair", "host": "h", "cause": "login_lapsed"}]


def test_a_host_with_login_ok_true_is_in_neither():
    host = _host("h", versions_json={"login_ok": True})
    assert login_blocked([host]) == set()
    assert login_needs_chair([host]) == []


def test_a_never_checked_host_with_login_ok_absent_is_not_blocked():
    host = _host("h", versions_json={})
    assert login_blocked([host]) == set()
    assert login_needs_chair([host]) == []


def test_merge_login_versions_adds_keys_without_mutating_input():
    versions = {"cox": "0.20.0"}
    merged = merge_login_versions(versions, True, "t")
    assert merged == {"cox": "0.20.0", "login_ok": True, "login_checked_at": "t"}
    assert versions == {"cox": "0.20.0"}


def test_versions_json_as_a_sqlite_json_string_is_decoded():
    stale = {"name": "s", "state": "active", "versions_json": json.dumps({"login_ok": False, "login_checked_at": "2026-09-27T11:29:00Z"})}
    fresh = {"name": "f", "state": "active", "versions_json": json.dumps({"login_ok": True, "login_checked_at": "2026-09-27T11:50:00Z"})}
    assert due_for_login_check([stale, fresh], NOW) == ["s"]
    assert login_blocked([stale, fresh]) == {"s"}
