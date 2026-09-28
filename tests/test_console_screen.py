from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_tools import console_screen
from agent_tools.draft_list import DraftRow
from agent_tools.run_store import Lane

_BEAT = "2026-09-28T12:50:33+00:00"
_NOW = datetime(2026, 9, 28, 12, 52, 33, tzinfo=UTC)
_EASTERN = ZoneInfo("America/New_York")


def _sections():
    return {
        "drafts": [DraftRow(id="foo", proposed_by="pat", age_seconds=3600)],
        "hosts": [{"name": "jarvis", "state": "up", "capacity": 2, "beat_at": _BEAT, "versions_json": "{}"}],
        "lanes": [Lane("run-1", "jarvis", "2026-09-28T00:00:00+00:00", _BEAT)],
        "chair": [{"session": "sess-1", "host": "jarvis", "pid": 123, "heartbeat_at": _BEAT}],
        "needs_chair": [{"kind": "needs_chair", "ts": _BEAT}],
    }


def test_render_marks_selected_draft():
    assert console_screen.render(_sections(), selected=0, width=80, now=_NOW, tz=_EASTERN) == [
        "drafts:",
        "> foo  pat  1h",
        "hosts:",
        "  jarvis  up  cap=2  beat=8:50 AM  2m ago  login ?",
        "lanes:",
        "  run-1  jarvis  beat=8:50 AM  2m ago",
        "chair:",
        "  sess-1  jarvis  pid=123",
        "needs chair:",
        "  ?  ?  8:50 AM  2m ago",
    ]


def test_render_formats_timestamps_as_local_clock_time_plus_age_not_the_raw_iso_string():
    lines = console_screen.render(_sections(), selected=-1, width=80, now=_NOW, tz=_EASTERN)
    host_line = next(line for line in lines if line.startswith("  jarvis"))
    assert "8:50 AM" in host_line
    assert "2m ago" in host_line
    assert _BEAT not in host_line


def test_host_beat_falls_back_to_never_when_absent():
    sections = {**_sections(), "hosts": [{"name": "jarvis", "state": "up", "capacity": 2, "beat_at": None, "versions_json": "{}"}]}
    lines = console_screen.render(sections, selected=-1, width=80, now=_NOW, tz=_EASTERN)
    assert "beat=never" in next(line for line in lines if line.startswith("  jarvis"))


def test_clock_and_age_formats_a_literal_timestamp():
    assert console_screen._clock_and_age(_BEAT, _NOW, _EASTERN) == "8:50 AM  2m ago"


def test_clock_and_age_returns_placeholder_for_unparseable_timestamp():
    assert console_screen._clock_and_age("not-a-timestamp", _NOW, _EASTERN) == "?"


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


def test_needs_chair_rows_collapse_to_the_newest_per_initiative_and_cause():
    older = {"kind": "needs_chair", "ts": "2026-09-28T07:09:36Z", "target": "i", "action_json": {"initiative": "i", "cause": "stranded"}}
    newer = {"kind": "needs_chair", "ts": "2026-09-28T07:18:55Z", "target": "i", "action_json": '{"initiative": "i", "cause": "stranded"}'}
    assert console_screen.newest_per_item([older, newer]) == [newer]


def test_gather_reads_drafts_from_the_workspace_work_directory(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(console_screen.draft_list, "read_drafts", lambda work_dir, now: seen.append(work_dir) or [])
    monkeypatch.setattr(console_screen.run_store, "hosts", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(console_screen.chair, "read", lambda runs_dir: None)
    monkeypatch.setattr(console_screen.chair_read_stale, "read_chair_actions", lambda runs_dir: [])
    console_screen.gather(tmp_path / "runs", tmp_path, "2026-09-28T12:00:00+00:00")
    assert seen == [tmp_path / "work"]
