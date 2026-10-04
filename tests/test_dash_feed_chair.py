import json
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import dash_feed

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_v1.json").read_text(encoding="utf-8"))

AT = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)  # 09-28 20:00 EDT; local midnight is 09-28 04:00Z
ROW = {"holder": "chair@omarchy:12345", "state": "live"}
LEASE = {"holder": "chair@omarchy:12345", "epoch": 7}
RECORD = {"claude_session": "a1b2c3d4-0000", "heartbeat_at": "2026-09-28T23:59:56+00:00"}
STATUS = ("09-28 19:59 EDT", "chair 09-28 19:59 EDT | landed 2, launched 1")
ACTION = {"kind": "land", "target": "dash-feed/p2-feed", "since": "2026-09-28T23:59:50Z"}
ROWS = [
    {"ts": "2026-09-28T20:00:00Z", "kind": "land", "status": "landed"},
    {"ts": "2026-09-28T21:00:00Z", "kind": "land_phase", "status": "landed"},
    {"ts": "2026-09-28T22:00:00Z", "kind": "launch_epic", "status": "done"},
    {"ts": "2026-09-28T22:30:00Z", "kind": "retry", "status": "failed"},
    {"ts": "2026-09-28T03:00:00Z", "kind": "land", "status": "landed"},  # before local midnight
]


def test_chair_section_from_staged_inputs_equals_the_fixture_chair_block():
    chair = dash_feed._chair_v1(ROW, LEASE, RECORD, AT, STATUS, ACTION, ROWS, 1)

    assert chair == FIXTURE["chair"]


def test_chair_section_with_no_action_running_has_a_null_current_action():
    chair = dash_feed._chair_v1(ROW, LEASE, RECORD, AT, STATUS, None, [], 0)

    assert chair["current_action"] is None
    assert chair["today"] == {"lands": 0, "launches": 0, "refused_or_failed": 0, "needs_chair_open": 0}
    assert {**chair, "current_action": ACTION, "today": FIXTURE["chair"]["today"]} == FIXTURE["chair"]


def test_chair_section_with_no_heartbeat_or_status_reads_zero_and_empty_strings():
    chair = dash_feed._chair_v1(ROW, LEASE, {}, AT)

    assert (chair["beat_age_s"], chair["last_tick_at"], chair["last_status"]) == (0, "", "")
