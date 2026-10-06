import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from agent_tools.chair_read_idle_lands import (
    gh_checks,
    land_waits,
    read_lands_waiting,
    status_text,
    unmerged_lands,
)
from agent_tools.forge_status import ForgeStatus

_PR = "https://github.com/o/r/pull/7"
_TS = "2026-10-05T01:00:00+00:00"
_NOW = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
_ROW = {"run": "r1", "task": "t", "pr": _PR, "steps_reached": ["pr_create", "wait_checks"], "ts": _TS}


def _bodies(*states: str) -> tuple[dict, dict]:
    runs = [
        {"name": f"c{i}", "status": s, "conclusion": "success" if s == "completed" else None}
        for i, s in enumerate(states)
    ]
    return {"total_count": len(runs), "check_runs": runs}, {"total_count": 0, "statuses": []}


def _log(tmp_path: Path, *rows: dict) -> Path:
    (tmp_path / "land.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path


def test_queued_checks_give_checks_started_false() -> None:
    assert land_waits([_ROW], {_PR: _bodies("queued")}, None) == [
        {"run": "r1", "pr": _PR, "waiting_since": _TS, "checks_started": False, "forge_status": None}
    ]


def test_one_running_check_gives_checks_started_true() -> None:
    assert land_waits([_ROW], {_PR: _bodies("queued", "in_progress")}, None)[0]["checks_started"] is True


def test_all_checks_complete_is_dropped() -> None:
    assert land_waits([_ROW], {_PR: _bodies("completed", "completed")}, None) == []


def test_an_empty_successful_read_is_not_started_and_not_unreadable() -> None:
    empty = ({"total_count": 0, "check_runs": []}, {"total_count": 0, "statuses": []})
    assert land_waits([_ROW], {_PR: empty}, None) == [
        {"run": "r1", "pr": _PR, "waiting_since": _TS, "checks_started": False, "forge_status": None}
    ]


def test_a_failed_read_is_checks_unreadable_with_the_error_text() -> None:
    assert land_waits([_ROW], {_PR: "gh: HTTP 502"}, None) == [
        {"run": "r1", "pr": _PR, "waiting_since": _TS, "checks_started": False,
         "forge_status": "checks unreadable: gh: HTTP 502"}
    ]


def test_a_failed_read_keeps_the_forge_status_text_after_it() -> None:
    waits = land_waits([_ROW], {_PR: "gh: HTTP 502"}, "Actions degraded")
    assert waits[0]["forge_status"] == "checks unreadable: gh: HTTP 502; Actions degraded"


def test_a_truncated_read_of_complete_checks_is_still_waiting() -> None:
    runs, status = _bodies("completed")
    assert land_waits([_ROW], {_PR: ({**runs, "total_count": 3}, status)}, None)[0]["checks_started"] is True


def test_a_merged_land_is_not_unmerged() -> None:
    merged = {**_ROW, "steps_reached": ["pr_create", "wait_checks", "merge"]}
    assert unmerged_lands([json.dumps(_ROW), json.dumps(merged)], _NOW) == []


def test_a_land_older_than_the_recent_window_is_skipped() -> None:
    assert unmerged_lands([json.dumps({**_ROW, "ts": "2026-09-01T00:00:00+00:00"})], _NOW) == []


def test_rows_are_capped_newest_first() -> None:
    rows = [
        json.dumps({**_ROW, "task": f"t{i}", "pr": f"{_PR}{i}", "ts": f"2026-10-05T01:0{i}:00+00:00"})
        for i in range(3)
    ]
    assert [r["task"] for r in unmerged_lands(rows, _NOW, cap=2)] == ["t2", "t1"]


def test_waiting_since_is_the_earliest_row_for_the_pr() -> None:
    later = {**_ROW, "ts": "2026-10-05T01:30:00+00:00"}
    assert unmerged_lands([json.dumps(_ROW), json.dumps(later)], _NOW)[0]["waiting_since"] == _TS


def test_a_status_text_is_carried_into_forge_status(tmp_path: Path) -> None:
    waits = read_lands_waiting(
        _log(tmp_path, _ROW), _NOW, lambda pr, t: _bodies("queued"), lambda t: "Actions degraded"
    )
    assert [w["forge_status"] for w in waits] == ["Actions degraded"]


def test_a_status_call_that_raises_gives_none_and_keeps_the_land(tmp_path: Path) -> None:
    def boom(timeout_s: float) -> str:
        raise TimeoutError

    waits = read_lands_waiting(_log(tmp_path, _ROW), _NOW, lambda pr, t: _bodies("queued"), boom)
    assert [(w["pr"], w["forge_status"]) for w in waits] == [(_PR, None)]


def test_a_check_read_that_raises_is_checks_unreadable(tmp_path: Path) -> None:
    def boom(pr: str, timeout_s: float) -> str:
        raise TimeoutError("slow")

    waits = read_lands_waiting(_log(tmp_path, _ROW), _NOW, boom)
    assert [w["forge_status"] for w in waits] == ["checks unreadable: TimeoutError: slow"]


def test_no_check_read_starts_after_the_budget(tmp_path: Path) -> None:
    calls: list[str] = []
    ticks = iter([0.0, 99.0])
    waits = read_lands_waiting(
        _log(tmp_path, _ROW), _NOW, lambda pr, t: calls.append(pr) or "x", budget_s=1.0, clock=lambda: next(ticks)
    )
    assert (calls, [w["forge_status"] for w in waits]) == ([], ["checks unreadable: budget exhausted"])


def test_an_unreadable_source_gives_an_empty_list(tmp_path: Path) -> None:
    assert read_lands_waiting(tmp_path / "missing", _NOW) == []


def test_a_degraded_forge_status_gives_its_incident_text() -> None:
    assert status_text(ForgeStatus(True, "Actions degraded", 0.0)) == "Actions degraded"
    assert status_text(ForgeStatus(False, "", 0.0)) is None


def test_gh_checks_reads_both_bodies_for_the_pr_repo() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kw: object) -> SimpleNamespace:
        seen.append(argv)
        out = "abc\n" if argv[1] == "pr" else ('{"check_runs": []}' if "check-runs" in argv[-1] else '{"statuses": []}')
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    assert gh_checks(_PR, 3.0, run) == ({"check_runs": []}, {"statuses": []})
    assert seen[1][-1] == "repos/o/r/commits/abc/check-runs?per_page=100"


def test_gh_checks_gives_the_error_text_on_a_failed_call_or_a_foreign_url() -> None:
    def run(argv: list[str], **kw: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502\n")

    assert gh_checks(_PR, 3.0, run) == "gh pr view failed: HTTP 502"
    assert gh_checks("https://example.com/o/r/pull/7", 3.0, run) == (
        "not a github pull request url: https://example.com/o/r/pull/7"
    )
