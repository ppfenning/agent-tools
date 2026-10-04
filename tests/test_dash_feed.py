import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_dash_detail_initiative import _INITIATIVE, _RAW_INITIATIVE
from test_dash_detail_machine import HOST_ROW, RUNS
from test_dash_detail_machine import NOW as _MACHINE_NOW
from test_dash_detail_run import _BASE_RAW

from agent_tools import console_screen, dash_feed, run_store, runs_top_screen, usage_meter, usage_window
from agent_tools.dash_detail_initiative import build_initiative_detail
from agent_tools.dash_detail_machine import build_machine_detail
from agent_tools.dash_detail_run import build_run_detail
from agent_tools.dash_feed import snapshot

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch):
    """`gather_feed` asks `claude auth status`; no test here may start that process."""
    monkeypatch.setattr(dash_feed, "_local_login", lambda local_name: None)


def _assert_matches_fixture_keys_and_nulls(output, fixture, path="root"):
    """`output` carries exactly `fixture`'s keys at every dict level `fixture` shows, and
    every value that is null in `output` is also null in `fixture` at the same path."""
    if isinstance(fixture, dict):
        assert isinstance(output, dict), path
        assert output.keys() == fixture.keys(), path
        for key, fixture_value in fixture.items():
            _assert_matches_fixture_keys_and_nulls(output[key], fixture_value, f"{path}.{key}")
    elif isinstance(fixture, list):
        assert isinstance(output, list), path
        for index, (output_item, fixture_item) in enumerate(zip(output, fixture, strict=True)):
            _assert_matches_fixture_keys_and_nulls(output_item, fixture_item, f"{path}[{index}]")
    elif output is None:
        assert fixture is None, path


def test_snapshot_matches_committed_fixture():
    at = "2026-09-29T00:00:00Z"
    chair = {
        "holder": "chair@omarchy:12345",
        "host": "omarchy",
        "epoch": 7,
        "liveness": "live",
        "beat_age_s": 4,
        "session": "a1b2c3d4",
        "last_tick_at": "09-28 19:59 EDT",
        "last_status": "chair 09-28 19:59 EDT | landed 2, launched 1",
        "current_action": {"kind": "land", "target": "dash-feed/p2-feed", "since": "2026-09-28T23:59:50Z"},
        "today": {"lands": 2, "launches": 1, "refused_or_failed": 1, "needs_chair_open": 1},
    }
    spend = {
        "five_hour_fraction": 0.16,
        "five_hour_source": "meter",
        "weekly_fraction": 0.35,
        "weekly_source": "meter",
        "hard_stop_fraction": 0.93,
        "five_hour_resets_at": "2026-09-29T02:00:00Z",
        "weekly_resets_at": "2026-10-04T04:00:00Z",
    }
    machines = [
        {
            "name": "omarchy",
            "state": "active",
            "lanes_in_use": 2,
            "capacity": 3,
            "login_ok": True,
            "login_checked_at": "2026-09-28T23:50:00Z",
            "beat_age_s": 12,
            "checkouts": {"coxswain-tools": {"behind_main": 0}},
        }
    ]
    runs = [
        {
            "run": "dash-feed-1",
            "machine": "omarchy",
            "phase": "p1-foundations",
            "node": "build",
            "attempt": 1,
            "turns": 12,
            "cost": 0.84,
            "verdict": "approve",
            "status": "running",
        }
    ]
    queue = [
        {
            "initiative": "dash-feed-streams-a-versioned-json-snapshot-of",
            "priority": 1,
            "phases_landed": 0,
            "phases_total": 3,
            "current_phase": "p1-foundations",
        }
    ]
    inbox = [
        {
            "kind": "needs_chair",
            "target": "some-task",
            "reason": "budget stop after 2 attempts",
        }
    ]
    watch = []
    decisions = [
        {
            "id": "d-1",
            "question": "Ship 0.27 with the dash decisions pane?",
            "options": ["ship", "hold"],
            "context": "The pane is behind a flag and the fixture is already in coxswain-dash.",
            "asked_at": "2026-09-29T00:00:00Z",
        }
    ]

    result = snapshot(at, chair, spend, machines, runs, queue, 1, inbox, 1, watch, decisions)

    with open("tests/fixtures/dash_feed_v1.json") as f:
        expected = json.load(f)

    assert result == expected


def _meter(five_pct: float, seven_pct: float, observed_at: datetime) -> usage_meter.Meter:
    return usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=five_pct, resets_at=datetime(2026, 9, 29, 2, tzinfo=UTC)),
        seven_day=usage_meter.MeterEntry(used_percentage=seven_pct, resets_at=datetime(2026, 10, 4, 4, tzinfo=UTC)),
        observed_at=observed_at,
    )


def test_spend_fresh_meter_wins(monkeypatch, tmp_path):
    now = "2026-09-29T00:10:00Z"
    meter = _meter(8, 58, datetime(2026, 9, 29, 0, 5, tzinfo=UTC))
    monkeypatch.setattr(dash_feed.usage_meter, "read", lambda: meter)

    spend = dash_feed._spend(tmp_path, now)

    assert spend["five_hour_fraction"] == 0.08
    assert spend["five_hour_source"] == "meter"
    assert spend["weekly_fraction"] == 0.58
    assert spend["weekly_source"] == "meter"


def _spy_readers(monkeypatch, window_spent: float, weekly_spent: float) -> dict:
    """Stand-ins for the two spend readers that take their real keyword names, keyword-only, and answer with the
    ceiling they were handed: a fraction can then only come from a ceiling that reached the reader."""
    seen: dict = {}

    def gather(runs_dir, now, *, ceiling_usd=None):
        seen["ceiling_usd"] = ceiling_usd
        return usage_window.Window(
            start=now, end=now, spent_usd=window_spent, ceiling_usd=ceiling_usd, burn_usd_per_hour=0.0, runs_in_flight=0,
        )

    def gather_weekly(runs_dir, now, *, weekly_ceiling_usd=None):
        seen["weekly_ceiling_usd"] = weekly_ceiling_usd
        return usage_window.Window(
            start=now, end=now, spent_usd=weekly_spent, ceiling_usd=weekly_ceiling_usd, burn_usd_per_hour=0.0,
            runs_in_flight=0,
        )

    monkeypatch.setattr(dash_feed.usage_window, "gather", gather)
    monkeypatch.setattr(dash_feed.usage_window, "gather_weekly", gather_weekly)
    return seen


def test_spend_estimate_uses_profile_ceiling(monkeypatch, tmp_path):
    now = "2026-09-29T00:00:00Z"
    stale = _meter(1, 1, datetime(2026, 9, 28, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(dash_feed.usage_meter, "read", lambda: stale)
    monkeypatch.setattr(dash_feed.usage_meter, "implied_ceiling", lambda kind, now: None)
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("spend:\n  window_ceiling_usd: 200\n  weekly_ceiling_usd: 400\n")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(profile_path))
    seen = _spy_readers(monkeypatch, window_spent=50.0, weekly_spent=100.0)

    spend = dash_feed._spend(tmp_path, now)

    assert seen == {"ceiling_usd": 200.0, "weekly_ceiling_usd": 400.0}
    assert spend["five_hour_fraction"] == 0.25
    assert spend["five_hour_source"] == "est"
    assert spend["weekly_fraction"] == 0.25
    assert spend["weekly_source"] == "est"


def test_spend_estimate_no_meter_no_profile(monkeypatch, tmp_path):
    now = "2026-09-29T00:00:00Z"
    monkeypatch.setattr(dash_feed.usage_meter, "read", lambda: None)
    monkeypatch.setattr(dash_feed.usage_meter, "implied_ceiling", lambda kind, now: None)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "missing.yaml"))
    seen = _spy_readers(monkeypatch, window_spent=50.0, weekly_spent=100.0)

    spend = dash_feed._spend(tmp_path, now)

    assert seen == {"ceiling_usd": None, "weekly_ceiling_usd": None}
    assert spend["five_hour_fraction"] is None
    assert spend["five_hour_source"] == "est"
    assert spend["weekly_fraction"] is None
    assert spend["weekly_source"] == "est"


def test_spend_reports_the_pacing_policys_weekly_hard_stop_fraction(monkeypatch, tmp_path):
    (tmp_path / "policy.pacing.json").write_text(json.dumps({"weekly_hard_stop_fraction": 0.98}))
    now = "2026-09-29T00:10:00Z"
    monkeypatch.setattr(dash_feed.usage_meter, "implied_ceiling", lambda kind, now: None)
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "missing.yaml"))
    _spy_readers(monkeypatch, window_spent=0.0, weekly_spent=0.0)

    fresh = _meter(8, 58, datetime(2026, 9, 29, 0, 5, tzinfo=UTC))
    monkeypatch.setattr(dash_feed.usage_meter, "read", lambda: fresh)
    from_meter = dash_feed._spend(tmp_path, now)
    monkeypatch.setattr(dash_feed.usage_meter, "read", lambda: None)
    from_estimate = dash_feed._spend(tmp_path, now)

    assert (from_meter["weekly_source"], from_estimate["weekly_source"]) == ("meter", "est")
    assert from_meter["hard_stop_fraction"] == 0.98
    assert from_estimate["hard_stop_fraction"] == 0.98


def test_gather_feed_calls_each_reader_once(monkeypatch, tmp_path):
    calls = {"console": 0, "queue": 0, "inbox": 0}

    def fake_console_gather(runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=None):
        calls["console"] += 1
        return {
            "hosts": [{"name": "omarchy"}],
            "lanes": [console_screen.LaneRow("dash-feed-1", None, "", "p1", "build", 1, 2, 0.1, 0, 2)],
            "chair": [{"holder": "chair@omarchy:1"}],
            "spend": spend,
        }

    def fake_read_queue(runs_dir):
        calls["queue"] += 1
        return [
            {"kind": "task", "initiative": "dash-feed", "phase": "p1", "state": "done", "extra": {"priority": 1}},
            {"kind": "task", "initiative": "dash-feed", "phase": "p2", "state": "todo", "extra": {}},
        ]

    def fake_inbox(blob, label=None, holder=None):
        calls["inbox"] += 1
        return [{"id": "m1", "from": "chair", "to": "pat", "note": "n", "ref": "coxswain://task/x", "ack": False}]

    monkeypatch.setattr(dash_feed, "_spend", lambda runs_dir, now: {"five_hour_fraction": 0.1})
    monkeypatch.setattr(dash_feed.console_screen, "gather", fake_console_gather)
    monkeypatch.setattr(dash_feed.run_store, "read_queue", fake_read_queue)
    monkeypatch.setattr(dash_feed.courier, "inbox", fake_inbox)

    result = dash_feed.gather_feed(tmp_path, tmp_path, "2026-09-29T00:00:00Z")

    assert calls == {"console": 1, "queue": 1, "inbox": 1}
    assert result["runs"][0]["run"] == "dash-feed-1"
    assert result["queue"][0]["initiative"] == "dash-feed"
    assert result["inbox"][0]["target"] == "x"

def test_the_live_feed_has_the_fixture_s_keys_no_nulls_and_serializes(monkeypatch, tmp_path):
    with open("tests/fixtures/dash_feed_v1.json") as f:
        fixture = json.load(f)
    (tmp_path / "chair.lease.json").write_text('{"holder": "chair-loop@omarchy:42", "epoch": 7}')
    (tmp_path / "chair.json").write_text('{"heartbeat_at": "2026-09-29T20:59:00+00:00"}')
    lane = console_screen.LaneRow(
        run="r-1", host=None, heartbeat_at="", phase="p1", node="build", attempt=1, turns=3, cost_usd=0.5,
        phases_landed=0, phases_total=2,
    )
    sections = {
        "chair": [{"holder": "chair-loop", "state": "live", "minutes_ago": 1}],
        "spend": dict.fromkeys(fixture["spend"]),
        "hosts": [{
            "name": "jarvis", "state": "draining", "capacity": 1, "in_use": 0, "beat_at": "2026-09-29T20:59:00Z",
            "versions_json": {"login_ok": True, "login_checked_at": "2026-09-29T20:41:15Z"},
        }],
        "lanes": [lane],
    }
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("omarchy", 3))
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir: ([
        {"initiative": "x", "priority": None, "phases_landed": 0, "phases_total": 1, "current_phase": None},
    ], 1))
    monkeypatch.setattr(dash_feed, "_inbox", lambda work_dir: ([
        {"ref": "coxswain://task/t1", "from": "chair-loop", "to": "chair", "note": "land it", "id": "m1", "ack": False},
    ], 1))

    feed = json.loads(json.dumps(dash_feed.gather_feed(tmp_path, tmp_path, "2026-09-29T21:00:00Z")))

    assert set(feed["chair"]) == set(fixture["chair"]) and set(feed["spend"]) == set(fixture["spend"])
    for section in ("machines", "runs", "queue", "inbox"):
        assert set(feed[section][0]) == set(fixture[section][0]), section
    rows = [feed["chair"], feed["spend"], *feed["machines"], *feed["runs"], *feed["queue"], *feed["inbox"]]
    # `current_action` is null when nothing is running; it is the one null the feed allows.
    assert all(value is not None for row in rows for key, value in row.items() if key != "current_action")
    assert (feed["chair"]["host"], feed["chair"]["epoch"], feed["chair"]["beat_age_s"]) == ("omarchy", 7, 60)
    assert (feed["runs"][0]["machine"], feed["runs"][0]["cost"]) == ("omarchy", 0.5)
    assert feed["machines"][0]["beat_age_s"] == 60
    assert feed["inbox"][0] == {"kind": "task", "target": "t1", "reason": "land it"}

    run_raw = {
        **_BASE_RAW,
        "timeline": [
            {"node": "plan", "attempt": 1, "turns": 4, "cost_usd": 0.12, "verdict": "approve"},
            {"node": "build", "attempt": 1, "turns": 12, "cost_usd": 0.84, "verdict": ""},
        ],
        "files": ["src/feed.rs", "tests/fixtures/dash_feed_v1.json"],
        "tool_calls": [
            {"tool": "bash", "summary": "cargo test", "at": "2026-09-29T11:58:00Z"},
            {"tool": "edit", "summary": "src/feed.rs", "at": "2026-09-29T11:55:00Z"},
        ],
        "log_tail": ["running 4 tests", "test feed::tests::parses_fixture ... ok", "test result: ok. 4 passed"],
    }
    run_output = build_run_detail(run_raw)
    run_fixture = json.loads((_FIXTURES_DIR / "dash_detail_run_v1.json").read_text(encoding="utf-8"))
    _assert_matches_fixture_keys_and_nulls(run_output, run_fixture)
    json.dumps(run_output)

    initiative_output = build_initiative_detail(_RAW_INITIATIVE, _INITIATIVE)
    initiative_fixture = json.loads((_FIXTURES_DIR / "dash_detail_initiative_v1.json").read_text(encoding="utf-8"))
    _assert_matches_fixture_keys_and_nulls(initiative_output, initiative_fixture)
    json.dumps(initiative_output)

    machine_output = build_machine_detail(HOST_ROW, RUNS, _MACHINE_NOW)
    machine_fixture = json.loads((_FIXTURES_DIR / "dash_detail_machine_v1.json").read_text(encoding="utf-8"))
    _assert_matches_fixture_keys_and_nulls(machine_output, machine_fixture)
    json.dumps(machine_output)


def _remote_feed_run(monkeypatch, calls, phases):
    """The feed's run row for one remote lane, with the store's usage and phase readers faked."""
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
    lane = run_store.Lane("x-3", "omarchy", "2026-10-04T11:40:00Z", "2026-10-04T11:59:50Z")
    monkeypatch.setattr(run_store, "usage", lambda root, run: {"calls": calls} if calls else None)
    monkeypatch.setattr(run_store, "phase_names", lambda root, run: phases)
    row = runs_top_screen._remote_rows(Path("/nonexistent"), [lane], now)[0]
    return dash_feed._run_v1(console_screen._lane_rows([lane], {"x-3": row}, [])[0], "bp-macbook")


def test_the_feed_row_of_a_remote_run_with_two_calls_reports_the_second_and_the_summed_cost(monkeypatch):
    calls = [
        {"role": "plan", "cost_usd": 0.25, "turns": 2, "ts": "2026-10-04T11:45:00Z"},
        {"role": "build", "cost_usd": 0.5, "turns": 5, "ts": "2026-10-04T11:50:00Z"},
    ]

    assert _remote_feed_run(monkeypatch, calls, ["p1", "p2"]) == {
        "run": "x-3", "machine": "omarchy", "phase": "p2", "node": "build", "attempt": 1, "turns": 7, "cost": 0.75,
        "verdict": "", "status": "running",
    }


def test_the_feed_row_of_a_remote_run_with_no_calls_says_starting(monkeypatch):
    row = _remote_feed_run(monkeypatch, [], [])

    assert (row["phase"], row["node"], row["attempt"], row["turns"], row["cost"]) == ("", "starting", 0, 0, 0.0)


def test_the_feed_row_of_a_local_lane_is_what_the_console_gave_it():
    lane = console_screen.LaneRow("r-1", None, "", "p1", "build", 1, 3, 0.5, 0, 2)

    assert dash_feed._run_v1(lane, "omarchy") == {
        "run": "r-1", "machine": "omarchy", "phase": "p1", "node": "build", "attempt": 1, "turns": 3, "cost": 0.5,
        "verdict": "", "status": "running",
    }


def test_queue_drops_a_fully_landed_initiative(monkeypatch, tmp_path):
    rows = [
        {"kind": "task", "initiative": "landed-one", "phase": "p1", "state": "done", "extra": {}},
        {"kind": "task", "initiative": "open-one", "phase": "p1", "state": "todo", "extra": {}},
    ]
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: rows)

    queue, total = dash_feed._queue(tmp_path)

    assert [row["initiative"] for row in queue] == ["open-one"]
    assert total == 1


def test_inbox_drops_an_entry_addressed_to_another_seat(monkeypatch, tmp_path):
    entries = [
        {"id": "1", "to": "chair", "note": "a", "ref": "coxswain://task/x", "ack": False, "from": "s"},
        {"id": "2", "to": "pat", "note": "b", "ref": "coxswain://task/y", "ack": False, "from": "s"},
        {"id": "3", "to": "build", "note": "c", "ref": "coxswain://task/z", "ack": False, "from": "s"},
        {"id": "4", "to": "arbitrate", "note": "d", "ref": "coxswain://task/z", "ack": False, "from": "s"},
        {"id": "5", "to": "review_adversary", "note": "e", "ref": "coxswain://task/z", "ack": False, "from": "s"},
        {"id": "6", "to": "cos", "note": "f", "ref": "coxswain://task/z", "ack": False, "from": "s"},
        {"id": "7", "to": "chair-2026-09-29", "note": "g", "ref": "coxswain://task/z", "ack": False, "from": "s"},
    ]
    monkeypatch.setattr(dash_feed.courier, "inbox", lambda blob: entries)

    inbox, total = dash_feed._inbox(dash_feed.courier.inbox(""))

    assert [entry["id"] for entry in inbox] == ["7", "2", "1"]
    assert total == 3


def test_queue_and_inbox_cap_at_fifty_rows_with_the_full_total(monkeypatch, tmp_path):
    rows = [
        {"kind": "task", "initiative": f"init-{i:02d}", "phase": "p1", "state": "todo", "extra": {}}
        for i in range(51)
    ]
    monkeypatch.setattr(dash_feed.run_store, "read_queue", lambda runs_dir: rows)
    entries = [
        {"id": str(i), "to": "chair", "note": "n", "ref": f"coxswain://task/t{i}", "ack": False, "from": "s"}
        for i in range(55)
    ]
    monkeypatch.setattr(dash_feed.courier, "inbox", lambda blob: entries)

    queue, queue_total = dash_feed._queue(tmp_path)
    inbox, inbox_total = dash_feed._inbox(dash_feed.courier.inbox(""))

    assert (len(queue), queue_total) == (50, 51)
    assert [row["initiative"] for row in queue] == [f"init-{i:02d}" for i in range(50)]
    assert (len(inbox), inbox_total) == (50, 55)
    assert inbox[0]["id"] == "54"


def _built_feed(monkeypatch, tmp_path):
    sections = {
        "chair": [{"holder": "chair-loop", "state": "live", "minutes_ago": 1}],
        "spend": {},
        "hosts": [],
        "lanes": [],
    }
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("omarchy", 3))
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir: ([], 0))
    return dash_feed.gather_feed(tmp_path, tmp_path, "2026-09-29T21:00:00Z")


def test_gather_feed_lists_only_the_open_decision(monkeypatch, tmp_path):
    bus = {"from": "chair", "to": "pat", "ack": False, "options": ["ship", "hold"]}
    entries = [
        bus | {"ref": "coxswain://decision/d-1", "id": "d-1", "note": "Ship it?", "context": "0.27 preview", "asked_at": "2026-09-29T20:00:00Z"},
        bus | {"ref": "coxswain://decision/d-2", "id": "d-2", "note": "Hold it?", "context": "0.26 preview", "asked_at": "2026-09-29T19:00:00Z"},
        {"ref": "coxswain://decision/d-2", "id": "a-2", "from": "pat", "to": "chair", "note": "answer: hold", "ack": False, "answer": "hold", "asked_at": "2026-09-29T20:30:00Z"},
    ]
    (tmp_path / "courier.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    feed = _built_feed(monkeypatch, tmp_path)

    assert feed["decisions"] == [
        {
            "id": "d-1",
            "question": "Ship it?",
            "options": ["ship", "hold"],
            "context": "0.27 preview",
            "asked_at": "2026-09-29T20:00:00Z",
        }
    ]


def test_an_acked_answer_still_closes_its_unacked_ask():
    ask = {"ref": "coxswain://decision/d-2", "id": "d-2", "from": "chair", "to": "pat", "note": "Hold it?",
           "ack": False, "options": ["ship", "hold"], "context": "c", "asked_at": "2026-09-29T19:00:00Z"}
    answer = {"ref": "coxswain://decision/d-2", "id": "a-2", "from": "pat", "to": "chair", "note": "answer: hold",
              "ack": True, "answer": "hold", "asked_at": "2026-09-29T20:30:00Z"}
    blob = "".join(json.dumps(entry) + "\n" for entry in (ask, answer))

    assert dash_feed._open_decisions(dash_feed.courier.inbox(blob), blob) == []


def test_the_chair_section_carries_the_first_eight_characters_of_claude_session(monkeypatch, tmp_path):
    (tmp_path / "chair.lease.json").write_text('{"holder": "chair-loop@omarchy:42", "epoch": 7}')
    (tmp_path / "chair.json").write_text(json.dumps({
        "session": "chair-loop", "pid": 42, "host": "omarchy", "taken_at": "2026-09-29T20:00:00+00:00",
        "heartbeat_at": "2026-09-29T20:59:00+00:00", "runs": [], "claude_session": "0123456789abcdef",
    }))

    assert _built_feed(monkeypatch, tmp_path)["chair"]["session"] == "01234567"


def test_a_chair_record_with_no_claude_session_yields_an_empty_session(monkeypatch, tmp_path):
    (tmp_path / "chair.lease.json").write_text('{"holder": "chair-loop@omarchy:42", "epoch": 7}')
    (tmp_path / "chair.json").write_text(json.dumps({
        "session": "chair-loop", "pid": 42, "host": "omarchy", "taken_at": "2026-09-29T20:00:00+00:00",
        "heartbeat_at": "2026-09-29T20:59:00+00:00", "runs": [],
    }))

    assert _built_feed(monkeypatch, tmp_path)["chair"]["session"] == ""


_CHECKED = "2026-10-04T00:00:00Z"


def _local_machine(login_ok):
    versions = dash_feed._local_versions(login_ok, _CHECKED)
    row = console_screen.with_local_host([], "omarchy", 1, versions)[0]
    return dash_feed._machine_v1({**row, "in_use": 0}, datetime(2026, 10, 4, tzinfo=UTC))


def test_the_local_row_reads_login_unknown_before_a_check():
    machine = _local_machine(None)

    assert (machine["login_ok"], machine["login_checked_at"]) == (None, "")


def test_the_local_row_reads_login_true_after_a_passing_check():
    machine = _local_machine(True)

    assert (machine["login_ok"], machine["login_checked_at"]) == (True, _CHECKED)


def test_a_check_that_says_not_logged_in_reads_false():
    assert _local_machine(False)["login_ok"] is False


def test_local_login_is_true_false_or_unknown_by_what_claude_auth_status_said(monkeypatch):
    def says(code, output):
        monkeypatch.setattr(dash_feed, "_run_capturing", lambda argv: (code, output))
        return dash_feed.chair_login_check._login_ok("m", "", dash_feed._run_capturing)

    assert [says(0, '{"loggedIn": true}'), says(0, '{"loggedIn": false}'), says(1, ""), says(0, "x")] == [
        True, False, None, None,
    ]


def test_gather_feed_seats_the_local_login_check_on_the_local_row(monkeypatch, tmp_path):
    def fake_gather(runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=None):
        hosts = console_screen.with_local_host([], local_name, local_capacity, local_versions)
        return {"hosts": hosts, "lanes": [], "chair": [], "spend": {}}

    monkeypatch.setattr(dash_feed.console_screen, "gather", fake_gather)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir: ([], 0))

    unchecked = dash_feed.gather_feed(tmp_path, tmp_path, _CHECKED)["machines"][0]
    monkeypatch.setattr(dash_feed, "_local_login", lambda local_name: True)
    checked = dash_feed.gather_feed(tmp_path, tmp_path, _CHECKED)["machines"][0]

    assert (unchecked["login_ok"], checked["login_ok"], checked["login_checked_at"]) == (None, True, _CHECKED)


def test_the_feeds_local_capacity_follows_the_cartridge_not_the_pacing_default(tmp_path):
    runs_dir, cartridges = tmp_path / "runs", tmp_path / "cartridges"
    (cartridges / "pat").mkdir(parents=True)
    runs_dir.mkdir()
    (cartridges / "pat" / "cartridge.yaml").write_text("policy:\n  dispatch:\n    max_in_flight: 1\n")
    (runs_dir / "policy.pacing.json").write_text('{"max_in_flight": 5}')
    profile = {"cartridges_dir": str(cartridges), "team": "pat"}

    assert dash_feed._local_identity(runs_dir, profile)[1] == 1
    assert dash_feed._local_identity(runs_dir, {})[1] == 5
