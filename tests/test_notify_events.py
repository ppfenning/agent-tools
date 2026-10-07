from agent_tools.notify_core import Event
from agent_tools.notify_events import events_from_tick

STRANDED = {"kind": "needs_chair", "initiative": "init-a", "cause": "stranded", "reason": "no patch"}
UNRELATED = {"kind": "needs_chair", "initiative": "init-b", "cause": "odd"}
LOGIN = {"kind": "needs_chair", "host": "lane1", "cause": "login_lapsed"}
STALLED = {
    "kind": "needs_chair",
    "initiative": "init-c",
    "run": "run-9",
    "cause": "stalled",
    "reason": "build t1, idle 45m",
}
IDLE = {
    "kind": "needs_chair",
    "initiative": "init-d",
    "cause": "idle_stall",
    "reason": "nothing moved",
    "signature": "sig1",
}


def _quarantine(initiative: str, failures: int) -> dict:
    return {"quarantines": [{"initiative": initiative, "task_id": "t1", "cause": "odd", "harness_failures": failures}]}


def test_stranded_gives_one_needs_chair_event():
    events = events_from_tick({}, [STRANDED])
    assert [(e.kind, e.key) for e in events] == [("needs_chair", "needs_chair:init-a:stranded")]
    assert "init-a" in events[0].title and "stranded" in events[0].body and "no patch" in events[0].body


def test_quarantine_with_two_failures_gives_one_event():
    events = events_from_tick(_quarantine("init-b", 2), [UNRELATED])
    assert [e.key for e in events] == ["needs_chair:init-b:odd"]


def test_quarantine_with_one_failure_gives_none():
    assert events_from_tick(_quarantine("init-b", 1), [UNRELATED]) == []


def test_quarantine_of_another_initiative_gives_none():
    assert events_from_tick(_quarantine("other", 5), [UNRELATED]) == []


def test_own_harness_failures_field_wins():
    action = {**UNRELATED, "harness_failures": 3}
    assert [e.key for e in events_from_tick(_quarantine("init-b", 0), [action])] == ["needs_chair:init-b:odd"]


def test_unrelated_cause_without_failures_gives_none():
    assert events_from_tick({"quarantines": []}, [UNRELATED]) == []


def test_login_with_url_clicks_through():
    events = events_from_tick({}, [{**LOGIN, "url": "https://login.example/x"}])
    assert events == [
        Event("host_login", "host_login:lane1", "Login lapsed on lane1", events[0].body, "https://login.example/x")
    ]


def test_login_without_url_names_the_login():
    (event,) = events_from_tick({}, [LOGIN])
    assert event.click == "" and "claude login" in event.body and "lane1" in event.body


def test_stalled_gives_stall_by_run():
    (event,) = events_from_tick({}, [STALLED])
    assert (event.kind, event.key) == ("stall", "stall:run-9")
    assert "run-9" in event.body and "init-c" in event.body and "idle 45m" in event.body


def test_idle_stall_keys_on_signature_then_initiative():
    (signed,) = events_from_tick({}, [IDLE])
    assert (signed.kind, signed.key, signed.body) == ("stall", "stall:idle:sig1", "nothing moved")
    (bare,) = events_from_tick({}, [{k: v for k, v in IDLE.items() if k != "signature"}])
    assert bare.key == "stall:idle:init-d"


def test_login_stalled_and_idle_never_give_needs_chair():
    facts = {"quarantines": [{"initiative": "init-c", "harness_failures": 9}, {"initiative": "init-d", "harness_failures": 9}]}
    events = events_from_tick(facts, [LOGIN, STALLED, IDLE])
    assert [e.kind for e in events] == ["host_login", "stall", "stall"]


def test_healthy_tick_gives_nothing():
    assert events_from_tick({}, [{"kind": "mark_lost", "initiative": "init-a"}, {"kind": "noop"}]) == []
    assert events_from_tick({}, []) == []


def test_action_order_kept_and_repeated_key_once():
    events = events_from_tick({}, [STALLED, LOGIN, STRANDED, LOGIN, STRANDED])
    assert [e.key for e in events] == ["stall:run-9", "host_login:lane1", "needs_chair:init-a:stranded"]
