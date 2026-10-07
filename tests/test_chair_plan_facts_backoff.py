from dataclasses import replace
from datetime import UTC, datetime

from test_chair_facts import _deps

from agent_tools.chair_facts import (
    gather_facts,
    land_refusals_from_actions,
    launch_history_from_actions,
    relaunch_loop_keys,
    resolve_max_launches_per_hour,
)
from agent_tools.chair_launch_budget import DEFAULT_MAX_LAUNCHES_PER_HOUR

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _land(ts: str, status: str, run: str = "x-2", phase: str = "p1") -> dict:
    return {"ts": ts, "kind": "land_phase", "status": status, "initiative": "x", "phase": phase, "run": run}


def test_attempts_count_refusals_after_the_last_landed_row_and_the_newest_stamp_wins():
    rows = [
        _land("2026-10-07T10:00:00Z", "refused"),
        _land("2026-10-07T10:30:00Z", "landed"),
        _land("2026-10-07T11:00:00Z", "failed"),
        _land("2026-10-07T11:30:00Z", "refused"),
    ]
    assert land_refusals_from_actions(rows) == [
        {"initiative": "x", "phase": "p1", "run": "x-2", "attempts": 2, "last_refused_at": "2026-10-07T11:30:00Z"}
    ]


def test_a_key_whose_last_row_is_landed_is_left_out_and_keys_stay_apart():
    rows = [
        _land("2026-10-07T10:00:00Z", "refused"),
        _land("2026-10-07T10:30:00Z", "landed"),
        _land("2026-10-07T11:00:00Z", "refused", run="x-3"),
    ]
    assert [(r["run"], r["attempts"]) for r in land_refusals_from_actions(rows)] == [("x-3", 1)]


def test_a_store_row_carries_its_land_in_action_json():
    row = {"ts": "1", "kind": "land_phase", "status": "refused",
           "action_json": '{"kind": "land_phase", "initiative": "x", "phase": "p", "run": "x-1"}'}
    assert land_refusals_from_actions([row]) == [
        {"initiative": "x", "phase": "p", "run": "x-1", "attempts": 1, "last_refused_at": "1"}
    ]


def _launch(ts: str, kind: str = "launch_epic", status: str = "done", initiative: str = "x") -> dict:
    return {"ts": ts, "kind": kind, "status": status, "initiative": initiative}


def test_launches_are_cut_at_one_hour_and_only_done_launch_kinds_count():
    rows = [
        _launch("2026-10-07T10:59:59Z"),
        _launch("2026-10-07T11:01:00Z", "relaunch"),
        _launch("2026-10-07T11:30:00Z", status="refused"),
        _launch("2026-10-07T11:40:00Z", "retry"),
    ]
    assert launch_history_from_actions(rows, [], NOW) == {
        "x": {"launches": [{"at": "2026-10-07T11:01:00Z", "kind": "relaunch"}], "runs": []}
    }


def test_runs_are_the_initiatives_quarantined_runs_newest_first_with_blank_inputs():
    quarantines = [
        {"initiative": "x", "task_id": "a", "run": "x-2", "reason": "boom"},
        {"initiative": "x", "task_id": "b", "run": "x-10", "reason": "bang"},
        {"initiative": "x", "task_id": "c", "reason": "no run"},
    ]
    history = launch_history_from_actions([], quarantines, NOW)
    assert history == {
        "x": {
            "launches": [],
            "runs": [
                {"run_id": "x-10", "quarantined": True, "reason": "bang", "body": "", "main_head": ""},
                {"run_id": "x-2", "quarantined": True, "reason": "boom", "body": "", "main_head": ""},
            ],
        }
    }


def test_relaunch_loop_keys_name_both_runs_from_the_reason_in_row_order():
    rows = [
        {"kind": "needs_chair", "cause": "relaunch_loop", "initiative": "x",
         "reason": "relaunch loop: runs x-4 and x-3 were both quarantined with 'boom'"},
        {"kind": "needs_chair", "cause": "decompose_stalled", "initiative": "x", "run": "x-1"},
        {"kind": "needs_chair", "cause": "relaunch_loop", "initiative": "x", "reason": "no runs named"},
    ]
    assert relaunch_loop_keys(rows) == ["x-4|x-3"]


def test_max_launches_per_hour_defaults_and_overrides():
    assert resolve_max_launches_per_hour(None) == DEFAULT_MAX_LAUNCHES_PER_HOUR
    assert resolve_max_launches_per_hour("many") == DEFAULT_MAX_LAUNCHES_PER_HOUR
    assert resolve_max_launches_per_hour(0) == DEFAULT_MAX_LAUNCHES_PER_HOUR
    assert resolve_max_launches_per_hour(True) == DEFAULT_MAX_LAUNCHES_PER_HOUR
    assert resolve_max_launches_per_hour(7.0) == 7


def test_gather_reads_actions_once_and_fills_the_four_keys():
    calls: list[int] = []
    rows = [_land("2026-10-07T11:00:00Z", "refused"), _launch("2026-10-07T11:30:00Z")]

    def actions() -> list[dict]:
        calls.append(1)
        return rows

    facts = gather_facts(replace(_deps(), actions=actions, max_launches_per_hour=lambda: 9), NOW)
    assert facts["land_refusals"][0]["attempts"] == 1
    assert facts["launch_history"]["x"]["launches"] == [{"at": "2026-10-07T11:30:00Z", "kind": "launch_epic"}]
    assert facts["relaunch_loop_reported"] == []
    assert facts["max_launches_per_hour"] == 9
    assert len(calls) == 1
    assert gather_facts(_deps(), NOW)["max_launches_per_hour"] == DEFAULT_MAX_LAUNCHES_PER_HOUR
