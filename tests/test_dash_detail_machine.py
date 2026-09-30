import json
from pathlib import Path

from agent_tools.dash_detail_machine import build_machine_detail

FIXTURE = Path(__file__).parent / "fixtures" / "dash_detail_machine_v1.json"

HOST_ROW = {
    "name": "omarchy",
    "state": "active",
    "capacity": 3,
    "beat_at": "2026-09-29T11:55:00+00:00",
    "versions_json": json.dumps({
        "cox": "1.2.3",
        "login_ok": True,
        "host_facts": {"os": "Linux 7.2.5-3-omarchy", "cpu_count": 16, "mem_total_gb": 64.0},
        "checkouts": {
            "coxswain-tools": {"branch": "main", "behind_main": 0},
            "coxswain-dash": {"branch": "worktree-coxtop-drills", "behind_main": 3},
        },
    }),
}

RUNS = [
    {
        "run_id": "run-a",
        "launched_at": "2026-09-28T09:00:00+00:00",
        "heartbeat_at": "2026-09-28T09:05:00+00:00",
        "ended_at": "2026-09-28T09:10:00+00:00",
    },
    {
        "run_id": "run-b",
        "launched_at": "2026-09-28T10:00:00+00:00",
        "heartbeat_at": "2026-09-28T11:59:00+00:00",
        "ended_at": None,
    },
    {
        "run_id": "run-c",
        "launched_at": "2026-09-29T08:00:00+00:00",
        "heartbeat_at": "2026-09-29T11:59:00+00:00",
        "ended_at": None,
    },
]

NOW = "2026-09-29T12:00:00Z"


def test_build_machine_detail_matches_the_v1_fixture_exactly():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert build_machine_detail(HOST_ROW, RUNS, NOW) == fixture


def test_lanes_in_use_counts_only_the_still_running_lanes_and_drops_the_list():
    result = build_machine_detail(HOST_ROW, RUNS, NOW)
    assert result["lanes_in_use"] == 2
    assert "lanes" not in result


def test_host_facts_default_to_empty_string_and_zero_when_a_beat_is_missing_a_field():
    row = {**HOST_ROW, "versions_json": json.dumps({"host_facts": {"os": "Linux", "cpu_count": 8}})}
    assert build_machine_detail(row, [], NOW)["host_facts"] == {"os": "Linux", "cpu_count": 8, "mem_total_gb": 0}


def test_host_facts_default_to_empty_string_and_zero_when_the_host_never_beat():
    row = {**HOST_ROW, "versions_json": "{}"}
    assert build_machine_detail(row, [], NOW)["host_facts"] == {"os": "", "cpu_count": 0, "mem_total_gb": 0}


def test_host_facts_default_when_there_is_no_host_row_at_all():
    assert build_machine_detail(None, [], NOW) == {
        "schema": 1,
        "kind": "machine",
        "at": NOW,
        "machine": None,
        "host_facts": {"os": "", "cpu_count": 0, "mem_total_gb": 0},
        "checkouts": {},
        "lanes_in_use": 0,
        "capacity": None,
    }


def test_build_machine_detail_round_trips_through_json_dumps():
    result = build_machine_detail(HOST_ROW, RUNS, NOW)
    assert json.loads(json.dumps(result)) == result
