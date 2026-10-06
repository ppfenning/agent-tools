import json
from pathlib import Path

from agent_tools import chair_read_idle_hosts as idle
from agent_tools import lane_hosts, run_store


def _row(name: str, **versions: object) -> dict:
    return {"name": name, "versions_json": json.dumps(versions)}


def test_failed_newest_result_is_not_ok_with_its_detail() -> None:
    rows = [_row("m1", login_ok=False, login_checked_at="2026-10-05T10:00:00Z", reason="missing KEY")]
    assert idle.host_checks(["m1"], rows) == [{"host": "m1", "ok": False, "detail": "missing KEY"}]


def test_success_after_an_older_failure_is_ok() -> None:
    rows = [_row("m1", login_ok=True, login_checked_at="2026-10-05T11:00:00Z")]
    assert idle.host_checks(["m1"], rows) == [{"host": "m1", "ok": True, "detail": ""}]


def test_host_with_no_result_is_ok() -> None:
    assert idle.host_checks(["m2"], [_row("m1", login_ok=False)]) == [{"host": "m2", "ok": True, "detail": ""}]


def test_recorded_timeout_is_not_ok_with_timed_out() -> None:
    rows = [_row("m1", login_ok=False, reason="timed out")]
    assert idle.host_checks(["m1"], rows) == [{"host": "m1", "ok": False, "detail": "timed out"}]


def test_failure_without_a_reason_gets_the_fixed_detail() -> None:
    assert idle.host_checks(["m1"], [_row("m1", login_ok=False)]) == [
        {"host": "m1", "ok": False, "detail": "login check failed"}
    ]


def test_edge_whose_source_raises_returns_empty(monkeypatch) -> None:
    def boom(runs_dir: Path) -> list[dict]:
        raise OSError("store gone")

    monkeypatch.setattr(run_store, "hosts", boom)
    host = lane_hosts.LaneHost("m1", "ssh m1", "/work")
    assert idle.read_idle_hosts(Path("/nowhere"), (host,)) == []
