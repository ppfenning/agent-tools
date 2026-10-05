import json

from agent_tools import console_screen, dash_feed, run_store


def test_the_feed_carries_each_runs_cost_series_and_todays_spend_series(monkeypatch, tmp_path):
    (tmp_path / "chair.lease.json").write_text('{"holder": "chair-loop@omarchy:42", "epoch": 7}')
    (tmp_path / "chair.json").write_text('{"heartbeat_at": "2026-10-04T11:59:00+00:00"}')
    # The local run's own usage records sit beside its pidfile.
    (tmp_path / "r-1.usage.json").write_text(json.dumps({"calls": [
        {"role": "plan", "cost_usd": 0.25, "turns": 2, "ts": "2026-10-04T11:45:00Z"},
        {"role": "build", "cost_usd": 0.5, "turns": 5, "ts": "2026-10-04T11:52:00Z"},
    ]}))
    # The remote run's calls come from the shared store; one of them is from yesterday.
    remote_calls = [
        {"role": "build", "cost_usd": 4.0, "turns": 9, "ts": "2026-10-03T23:00:00Z"},
        {"role": "review", "cost_usd": 0.125, "turns": 1, "ts": "2026-10-04T11:57:00Z"},
    ]
    real_usage = run_store.usage
    monkeypatch.setattr(
        run_store, "usage",
        lambda root, run: {"calls": remote_calls} if run == "r-2" else real_usage(root, run),
    )
    monkeypatch.setattr(dash_feed, "exit_rows", lambda runs_dir: [])
    sections = {
        "chair": [{"holder": "chair-loop", "state": "live", "minutes_ago": 1}],
        "spend": {},
        "hosts": [],
        "lanes": [
            console_screen.LaneRow("r-1", None, "", "p1", "build", 1, 7, 0.75, 0, 2),
            console_screen.LaneRow("r-2", "omarchy", "", "p1", "review", 1, 10, 4.125, 0, 2),
        ],
    }
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("omarchy", 3))
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir: ([], 0))

    feed = json.loads(json.dumps(dash_feed.gather_feed(tmp_path, tmp_path, "2026-10-04T12:00:00Z")))

    assert feed["schema"] == 1
    assert feed["runs"][0]["cost_series"] == [
        ["2026-10-04T11:45:00Z", 0.25, "plan"],
        ["2026-10-04T11:52:00Z", 0.75, "build"],
    ]
    assert feed["spend_series"] == [["2026-10-04T11:40:00Z", 0.25], ["2026-10-04T11:50:00Z", 0.875]]
