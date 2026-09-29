import json
from datetime import UTC, datetime

from agent_tools import dash_feed, usage_meter, usage_window
from agent_tools.dash_feed import snapshot


def test_snapshot_matches_committed_fixture():
    at = "2026-09-29T00:00:00Z"
    chair = {
        "holder": "chair@omarchy:12345",
        "host": "omarchy",
        "epoch": 7,
        "liveness": "live",
        "beat_age_s": 4,
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

    result = snapshot(at, chair, spend, machines, runs, queue, inbox, watch)

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


def test_gather_feed_calls_each_reader_once(monkeypatch, tmp_path):
    calls = {"console": 0, "queue": 0, "inbox": 0}

    def fake_console_gather(runs_dir, work_dir, now, local_name, local_capacity, spend):
        calls["console"] += 1
        return {
            "hosts": [{"name": "omarchy"}],
            "lanes": [{"run": "dash-feed-1"}],
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

    assert result == snapshot(
        "2026-09-29T00:00:00Z",
        {"holder": "chair@omarchy:1"},
        {"five_hour_fraction": 0.1},
        [{"name": "omarchy"}],
        [{"run": "dash-feed-1"}],
        [
            {
                "initiative": "dash-feed",
                "priority": 1,
                "phases_landed": 1,
                "phases_total": 2,
                "current_phase": "p2",
            }
        ],
        [{"id": "m1", "from": "chair", "to": "pat", "note": "n", "ref": "coxswain://task/x", "ack": False}],
        [],
    )
    assert calls == {"console": 1, "queue": 1, "inbox": 1}
