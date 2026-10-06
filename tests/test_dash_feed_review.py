import json
from pathlib import Path

from agent_tools import dash_feed
from agent_tools.dash_feed import _review_inbox_v1

PR = {
    "initiative": "dash-feed", "phase": "p2-feed", "task_id": "p2-feed-task", "repo": "coxswain-tools",
    "url": "https://github.com/pat/coxswain-tools/pull/42", "state": "unknown", "merged_at": None,
}


def test_the_inbox_over_one_awaiting_task_carries_the_fixture_entry():
    fixture = json.loads(Path("tests/fixtures/dash_feed_v1.json").read_text(encoding="utf-8"))
    assert _review_inbox_v1([PR], {"dash-feed": "I412"}) == [e for e in fixture["inbox"] if e["kind"] == "review_pr"]


def test_gather_feed_lists_an_awaiting_pr_with_its_url_and_never_asks_the_forge(monkeypatch, tmp_path):
    task = tmp_path / "dash-feed-1" / "tasks" / "p2-feed"
    task.mkdir(parents=True)
    (task / "p2-feed-task.json").write_text(json.dumps({
        "status": "approved", "initiative": "dash-feed", "task_id": "p2-feed-task", "repo": "coxswain-tools",
        "review_pr": PR["url"],
    }))
    sections = {"chair": [], "spend": {}, "hosts": [], "lanes": []}
    monkeypatch.setattr(dash_feed.console_screen, "gather", lambda *args, **kwargs: sections)
    monkeypatch.setattr(dash_feed, "_spend", lambda *args: {})
    monkeypatch.setattr(dash_feed, "_local_identity", lambda runs_dir, profile: ("omarchy", 3))
    monkeypatch.setattr(dash_feed, "_local_login", lambda name: None)
    monkeypatch.setattr(dash_feed, "_queue", lambda runs_dir: ([], 0))
    monkeypatch.setattr(dash_feed, "_courier_blob", lambda work_dir: "")
    monkeypatch.setattr(dash_feed.run_store, "initiative_short_ids", lambda runs_dir: {})

    def forbidden(*args, **kwargs):
        raise AssertionError("the feed must not call the forge or a subprocess")

    monkeypatch.setattr(dash_feed.subprocess, "run", forbidden)
    feed = dash_feed.gather_feed(tmp_path, tmp_path, "2026-09-29T21:00:00Z")
    assert feed["inbox"] == [_review_inbox_v1([PR], {})[0]]
    assert feed["inbox_total"] == 1
