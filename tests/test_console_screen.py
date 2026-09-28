from pathlib import Path

from agent_tools import console_screen
from agent_tools.draft_list import DraftRow
from agent_tools.run_store import Lane


def _sections():
    return {
        "drafts": [DraftRow(id="foo", proposed_by="pat", age_seconds=3600)],
        "hosts": [{"name": "jarvis", "state": "up", "capacity": 2, "beat_at": "t", "versions_json": "{}"}],
        "lanes": [Lane("run-1", "jarvis", "2026-09-28T00:00:00+00:00", "2026-09-28T00:05:00+00:00")],
        "chair": [{"session": "sess-1", "host": "jarvis", "pid": 123, "heartbeat_at": "2026-09-28T00:05:00+00:00"}],
        "needs_chair": [{"kind": "needs_chair", "ts": "2026-09-28T00:00:00+00:00"}],
    }


def test_render_marks_selected_draft():
    assert console_screen.render(_sections(), selected=0, width=80) == [
        "drafts:",
        "> foo  pat  1h",
        "hosts:",
        "  jarvis  up  cap=2",
        "lanes:",
        "  run-1  jarvis  beat=2026-09-28T00:05:00+00:00",
        "chair:",
        "  sess-1  jarvis  pid=123",
        "needs chair:",
        "  2026-09-28T00:00:00+00:00  needs_chair",
    ]


def test_selection_at_host_index():
    sections = {
        "drafts": [DraftRow(id="foo", proposed_by="pat", age_seconds=3600)],
        "hosts": [{"name": "jarvis"}],
        "lanes": [],
        "chair": [],
    }
    assert console_screen.selection_at(sections, 1) == {"kind": "host", "name": "jarvis"}


def test_selection_at_past_last_item_is_none():
    sections = {
        "drafts": [DraftRow(id="foo", proposed_by="pat", age_seconds=3600)],
        "hosts": [{"name": "jarvis"}],
        "lanes": [],
        "chair": [],
    }
    assert console_screen.selection_at(sections, 2) is None


def test_gather_keeps_only_recent_needs_chair(monkeypatch):
    now = "2026-09-28T12:00:00+00:00"
    recent = {"kind": "needs_chair", "ts": "2026-09-28T11:55:00+00:00"}
    stale = {"kind": "needs_chair", "ts": "2026-09-28T00:00:00+00:00"}
    keyless = {"ts": "2026-09-28T11:59:00+00:00"}

    monkeypatch.setattr(console_screen.draft_list, "read_drafts", lambda work_dir, now: ["draft-row"])
    monkeypatch.setattr(console_screen.run_store, "hosts", lambda runs_dir: ["host-row"])
    monkeypatch.setattr(console_screen.run_store, "live_lanes", lambda runs_dir, now: ["lane-row"])
    monkeypatch.setattr(console_screen.chair, "read", lambda runs_dir: {"session": "sess-1"})
    monkeypatch.setattr(console_screen.chair_read_stale, "read_chair_actions", lambda runs_dir: [recent, stale, keyless])

    result = console_screen.gather(Path("/runs"), Path("/work"), now)

    assert result == {
        "drafts": ["draft-row"],
        "hosts": ["host-row"],
        "lanes": ["lane-row"],
        "chair": [{"session": "sess-1"}],
        "needs_chair": [recent],
    }
