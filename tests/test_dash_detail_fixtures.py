import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def test_dash_detail_run_v1_fixture_loads():
    with open(FIXTURES / "dash_detail_run_v1.json") as fh:
        data = json.load(fh)
    assert data["schema"] == 1
    assert data["kind"] == "run"
    assert data["at"] == "2026-09-29T12:00:00Z"
    assert data["run"] == "dash-feed-1"
    assert data["machine"] == "omarchy"
    assert data["initiative"] == "dash-feed-streams-a-versioned-json-snapshot-of"
    assert data["phase"] == "p1-foundations"
    assert isinstance(data["steps"], list)
    assert data["stopped_reason"] is None
    assert isinstance(data["files"], list)
    assert isinstance(data["last_tool_calls"], list)
    assert isinstance(data["log_tail"], list)


def test_dash_detail_initiative_v1_fixture_loads():
    with open(FIXTURES / "dash_detail_initiative_v1.json") as fh:
        data = json.load(fh)
    assert data["schema"] == 1
    assert data["kind"] == "initiative"
    assert data["at"] == "2026-09-29T12:00:00Z"
    assert data["initiative"] == "dash-feed-streams-a-versioned-json-snapshot-of"
    assert isinstance(data["phases"], list)
    assert isinstance(data["history"], list)


def test_dash_detail_machine_v1_fixture_loads():
    with open(FIXTURES / "dash_detail_machine_v1.json") as fh:
        data = json.load(fh)
    assert data["schema"] == 1
    assert data["kind"] == "machine"
    assert data["at"] == "2026-09-29T12:00:00Z"
    assert data["machine"] == "omarchy"
    assert isinstance(data["host_facts"], dict)
    assert isinstance(data["checkouts"], dict)
    assert data["lanes_in_use"] == 2
    assert data["capacity"] == 3
