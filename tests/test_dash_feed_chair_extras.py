import json
from datetime import datetime

from agent_tools.dash_feed import _chair_v1
from agent_tools.draft_list import DraftRow

_FIELDS = ("lands_today", "phases_today", "needs_you", "drafts", "housekeeping_age_s")
_AT = datetime.fromisoformat("2026-09-29T00:00:00+00:00")


def test_chair_extras_from_staged_inputs_match_the_fixture():
    rows = [
        {"kind": "land", "status": "landed", "ts": "2026-09-28T23:00:00+00:00"},
        {"kind": "land_phase", "status": "landed", "ts": "2026-09-28T23:30:00+00:00"},
        {"kind": "land", "status": "landed", "ts": "2026-09-27T12:00:00+00:00"},
    ]
    entries = [{"to": "pat"}, {"to": "chair"}, {"to": "chair-2026-09-29"}]
    drafts = [DraftRow("a", "chair", 60), DraftRow("b", "chair", None)]
    chair = _chair_v1(
        {}, {}, {}, _AT, action_rows=rows, inbox_entries=entries, drafts=drafts,
        last_housekeeping_at="2026-09-28T23:00:00+00:00",
    )
    with open("tests/fixtures/dash_feed_v1.json") as f:
        expected = json.load(f)["chair"]
    assert {key: chair[key] for key in _FIELDS} == {key: expected[key] for key in _FIELDS}


def test_chair_extras_with_empty_inputs_are_zero():
    chair = _chair_v1({}, {}, {}, _AT)
    assert {key: chair[key] for key in _FIELDS} == dict.fromkeys(_FIELDS, 0)
