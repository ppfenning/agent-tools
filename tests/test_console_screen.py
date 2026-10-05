from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_tools import console_screen, runs_top
from agent_tools.draft_list import DraftRow
from agent_tools.run_store import Lane

_BEAT = "2026-09-28T12:50:33+00:00"
_NOW = datetime(2026, 9, 28, 12, 52, 33, tzinfo=UTC)
_EASTERN = ZoneInfo("America/New_York")


def _sections():
    return {
        "drafts": [DraftRow(id="foo", proposed_by="pat", age_seconds=3600)],
        "hosts": [{"name": "jarvis", "state": "up", "capacity": 3, "in_use": 2, "beat_at": _BEAT, "versions_json": "{}"}],
        "lanes": [console_screen.LaneRow(run="run-1", host="jarvis", heartbeat_at=_BEAT, phase="build", node="claude",
                                         attempt=2, turns=14, cost_usd=1.5, phases_landed=1, phases_total=2)],
        "chair": [{"holder": "sess-1", "state": "live", "minutes_ago": 2}],
        "spend": {"five_hour": 0.4, "weekly": 0.12, "hard_stop": 0.93},
        "needs_chair": [{"kind": "needs_chair", "ts": _BEAT}],
    }


def test_render_marks_selected_draft():
    assert console_screen.render(_sections(), selected=0, width=100, now=_NOW, tz=_EASTERN) == [
        "spend: 5h 40%  weekly 12%/93%",
        "drafts:",
        "> foo  pat  1h",
        "hosts:",
        "  jarvis  2/3  up  beat=8:50 AM  2m ago  login ?",
        "lanes:",
        "  run-1  jarvis  beat=8:50 AM  2m ago  build  claude  att 2  turns 14  $1.50  1/2 phases",
        "chair:",
        "  sess-1  (live, beat 2m ago)",
        "needs chair:",
        "  ?  ?  8:50 AM  2m ago",
    ]


def test_render_shows_spend_as_not_available_when_a_ceiling_is_missing():
    sections = {**_sections(), "spend": {"five_hour": None, "weekly": None, "hard_stop": 0.93}}
    lines = console_screen.render(sections, selected=-1, width=80, now=_NOW, tz=_EASTERN)
    assert lines[0] == "spend: 5h n/a  weekly n/a"


def test_render_omits_the_spend_line_when_no_spend_section_is_given():
    sections = {k: v for k, v in _sections().items() if k != "spend"}
    lines = console_screen.render(sections, selected=-1, width=80, now=_NOW, tz=_EASTERN)
    assert lines[0] == "drafts:"


def test_with_local_host_appends_a_synthetic_row_when_absent():
    hosts = [{"name": "jarvis", "capacity": 2, "state": "up"}]
    assert console_screen.with_local_host(hosts, "omarchy", 3) == [
        {"name": "jarvis", "capacity": 2, "state": "up"},
        {"name": "omarchy", "capacity": 3, "state": "active"},
    ]


def test_with_local_host_leaves_hosts_unchanged_when_the_local_row_already_exists():
    hosts = [{"name": "omarchy", "capacity": 4, "state": "up"}]
    assert console_screen.with_local_host(hosts, "omarchy", 3) == hosts


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
    host_row = {"name": "jarvis", "state": "up", "capacity": 2}
    lane_row = Lane("run-1", "jarvis", "2026-09-28T00:00:00+00:00", _BEAT)
    chair_row = {"holder": "sess-1", "state": "live", "minutes_ago": 0}
    spend = {"five_hour": None, "weekly": None, "hard_stop": 0.93}

    monkeypatch.setattr(console_screen.draft_list, "read_drafts", lambda work_dir, now: ["draft-row"])
    monkeypatch.setattr(console_screen.run_store, "hosts", lambda runs_dir: [host_row])
    monkeypatch.setattr(console_screen.run_store, "live_lanes", lambda runs_dir, now: [lane_row])
    monkeypatch.setattr(console_screen.runs_top_screen, "chair_now", lambda runs_dir: chair_row)
    monkeypatch.setattr(console_screen.runs_top_screen, "rows_now", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.run_store, "work_items", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.chair_read_stale, "read_chair_actions", lambda runs_dir: [recent, stale, keyless])

    result = console_screen.gather(Path("/runs"), Path("/work"), now, "omarchy", 3, spend)

    assert result == {
        "drafts": ["draft-row"],
        "hosts": [{**host_row, "in_use": 1}, {"name": "omarchy", "capacity": 3, "state": "active", "in_use": 0}],
        "lanes": [console_screen.LaneRow(run="run-1", host="jarvis", heartbeat_at=_BEAT, phase="", node="",
                                          attempt=0, turns=0, cost_usd=0.0, phases_landed=0, phases_total=0)],
        "chair": [chair_row],
        "spend": spend,
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
    monkeypatch.setattr(console_screen.runs_top_screen, "chair_now", lambda runs_dir: None)
    monkeypatch.setattr(console_screen.runs_top_screen, "rows_now", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.chair_read_stale, "read_chair_actions", lambda runs_dir: [])
    spend = {"five_hour": None, "weekly": None, "hard_stop": 0.93}
    console_screen.gather(tmp_path / "runs", tmp_path, "2026-09-28T12:00:00+00:00", "omarchy", 3, spend)
    assert seen == [tmp_path / "work"]


def test_lane_line_shows_its_own_runs_top_phase_and_its_initiatives_phase_progress():
    """The PHASE is the run's own `rows_now` row, never a value inferred from row order; progress is a count
    of phases whose items are all `done` or `dropped`."""
    lane = Lane("acme-3", "jarvis", "2026-09-28T00:00:00+00:00", _BEAT)
    run_rows = {"acme-3": runs_top.Row(run="acme-3", alive=True, phase="build", node="claude", attempt=2,
                                        turns=14, cost_usd=1.5, verdict="", status="running")}
    items = [
        {"initiative": "acme", "phase": "plan", "state": "done"},
        {"initiative": "acme", "phase": "plan", "state": "dropped"},
        {"initiative": "acme", "phase": "build", "state": "open"},
    ]

    rows = console_screen._lane_rows([lane], run_rows, items)
    line = console_screen._lane_line(rows[0], _NOW, _EASTERN)

    assert "build" in line
    assert "1/2 phases" in line


def test_lane_line_shows_the_runs_short_id_and_the_run_key_when_it_has_none():
    lane = Lane("acme-20261005-long-slug", "jarvis", "2026-09-28T00:00:00+00:00", _BEAT)
    with_id = runs_top.Row(run=lane.run, alive=True, phase="build", node="claude", attempt=1, turns=1, cost_usd=0.0,
                           verdict="", status="running", short_id="I7-2")
    without = runs_top.Row(run=lane.run, alive=True, phase="build", node="claude", attempt=1, turns=1, cost_usd=0.0,
                           verdict="", status="running")

    short = console_screen._lane_line(console_screen._lane_rows([lane], {lane.run: with_id}, [])[0], _NOW, _EASTERN)
    keyed = console_screen._lane_line(console_screen._lane_rows([lane], {lane.run: without}, [])[0], _NOW, _EASTERN)

    assert short.startswith("I7-2  ")
    assert lane.run not in short
    assert keyed.startswith(lane.run)


def test_gather_reads_work_items_once_per_snapshot_window(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(console_screen.draft_list, "read_drafts", lambda work_dir, now: [])
    monkeypatch.setattr(console_screen.run_store, "hosts", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.run_store, "live_lanes", lambda runs_dir, now: [])
    monkeypatch.setattr(console_screen.runs_top_screen, "chair_now", lambda runs_dir: None)
    monkeypatch.setattr(console_screen.runs_top_screen, "rows_now", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.chair_read_stale, "read_chair_actions", lambda runs_dir: [])
    monkeypatch.setattr(console_screen.run_store, "work_items", lambda runs_dir: calls.append(runs_dir) or [])
    spend = {"five_hour": None, "weekly": None, "hard_stop": 0.93}
    runs_dir = tmp_path / "runs"

    console_screen.gather(runs_dir, tmp_path, "2026-09-28T12:00:00+00:00", "omarchy", 3, spend)
    console_screen.gather(runs_dir, tmp_path, "2026-09-28T12:00:00+00:00", "omarchy", 3, spend)

    assert len(calls) == 1
