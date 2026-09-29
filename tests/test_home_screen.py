import threading
import time
from datetime import UTC, datetime, timedelta

from agent_tools import home_layout, home_model, leader_chat, pacing, theme, usage_meter
from agent_tools.home_model import Span
from agent_tools.home_screen import (
    _key_for,
    _paint,
    _panel,
    _read_window,
    _read_with_timeout,
    _send_chat,
    draw,
    facts,
    load_layout,
    run_effect,
    save_layout,
)

_PAIRS = theme.pair_numbers(theme.default)


def test_panel_status_is_fresh_within_timeout_stale_past_it_and_absent_with_no_value():
    assert home_model.panel_status("v", 1.0, 2.0) == "fresh"
    assert home_model.panel_status("v", 3.0, 2.0) == "stale"
    assert home_model.panel_status(None, 0.0, 2.0) == "absent"


def test_read_with_timeout_returns_none_without_waiting_for_a_blocked_reader():
    release = threading.Event()
    try:
        result = _read_with_timeout(release.wait, timeout_seconds=0.05)

        assert result is None
        assert not release.is_set()  # the reader was still blocked when the call returned
    finally:
        release.set()


def test_read_with_timeout_returns_none_for_a_raising_reader():
    def _raise():
        raise ValueError("boom")

    assert _read_with_timeout(_raise, timeout_seconds=0.05) is None


def test_panel_falls_back_to_the_cached_value_and_marks_stale_when_the_reader_times_out():
    cache = {"window": ({"tier": "standard"}, 100.0)}

    value, status, new_cache = _panel(cache, "window", lambda: time.sleep(0.5), timeout_seconds=0.05, now=105.0)

    assert value == {"tier": "standard"}
    assert status == "stale"
    assert new_cache == cache


def test_facts_reads_one_run_and_treats_a_missing_leader_file_as_none(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "r1.pid").write_text("123", encoding="utf-8")
    (runs_dir / "r1.log").write_text("n1 verdict: land\n", encoding="utf-8")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()

    result, cache = facts(runs_dir, work_dir, intake_dir, now=1_700_000_000.0)

    assert result.leader is None
    assert len(result.runs_rows) == 1
    assert cache["_status"]["leader"] == "fresh"


def test_facts_keeps_the_cached_window_when_its_reader_times_out(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    stale_window = {"tier": "cheap", "effort_ceiling": "low", "spent_usd": 1.0, "time_to_reset": "1m", "reason": "r"}
    cache = {"window": (stale_window, 0.0)}
    monkeypatch.setattr("agent_tools.home_screen._read_window", lambda *a, **k: time.sleep(0.5))

    result, new_cache = facts(runs_dir, work_dir, intake_dir, now=10.0, cache=cache, timeout_seconds=0.05)

    assert result.window == stale_window
    assert new_cache["_status"]["window"] == "stale"


def _estimate_window(spent: float, now_dt: datetime) -> pacing.Window:
    return pacing.Window(
        start=now_dt - timedelta(hours=1), end=now_dt,
        spent_usd=spent, ceiling_usd=100.0, burn_usd_per_hour=spent, runs_in_flight=0,
    )


def test_read_window_prefers_a_fresh_meter_over_the_estimate(tmp_path, monkeypatch):
    now_dt = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    meter = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(used_percentage=16.0, resets_at=now_dt + timedelta(hours=3)),
        seven_day=usage_meter.MeterEntry(used_percentage=35.0, resets_at=now_dt + timedelta(days=5)),
        observed_at=now_dt,
    )
    monkeypatch.setattr("agent_tools.home_screen.usage_meter.read", lambda *a, **k: meter)
    monkeypatch.setattr(
        "agent_tools.home_screen.usage_window.gather", lambda *a, **k: _estimate_window(2.0, now_dt),
    )

    result = _read_window(tmp_path, now_dt)

    assert result["spent_usd"] == 16.0
    assert result["time_to_reset"] == "180m"
    assert result["block_left"] == 0.6
    assert result["source"] == "meter"


def test_read_window_falls_back_to_the_estimate_when_the_meter_is_absent(tmp_path, monkeypatch):
    now_dt = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    monkeypatch.setattr("agent_tools.home_screen.usage_meter.read", lambda *a, **k: None)
    monkeypatch.setattr(
        "agent_tools.home_screen.usage_window.gather", lambda *a, **k: _estimate_window(2.0, now_dt),
    )

    result = _read_window(tmp_path, now_dt)

    assert result["spent_usd"] == 2.0
    assert result["source"] == "estimate"


class _FakeStdscr:
    def __init__(self, size=(24, 80)):
        self._size = size
        self.addnstr_calls = []

    def getmaxyx(self):
        return self._size

    def clear(self):
        pass

    def refresh(self):
        pass

    def addnstr(self, y, x, s, n, attr=0):
        self.addnstr_calls.append((y, x, s, n, attr))


def _facts_fixture():
    return home_model.Facts(
        leader={"session": "s1", "heartbeat_at": "2024-01-01T00:00:00+00:00"},
        leader_liveness="live",
        runs_rows=(),
        backlog={"queued": 1, "decomposed": 2, "landed": 3, "ready": {"init-a": 1}},
        window={"tier": "standard", "effort_ceiling": "high", "spent_usd": 1.5, "time_to_reset": "10m", "reason": "on pace"},
        now=1_704_067_200.0,
    )


def test_draw_contains_each_panes_expected_lines_and_marks_a_stale_panel():
    stdscr = _FakeStdscr()
    state = home_model.State(plugin_dir="/plugins/coxswain", leader_liveness="live", other_holder=None)
    statuses = {"leader": "fresh", "runs": "fresh", "backlog": "stale", "window": "fresh"}

    draw(stdscr, _facts_fixture(), state, statuses, _PAIRS)

    text = [call[2] for call in stdscr.addnstr_calls]
    assert any("Leader" in line for line in text)
    assert any("queued 1" in line for line in text)
    assert any("Window" in line for line in text)
    assert any("! Backlog" in line for line in text)


def test_draw_marks_only_the_stale_panel_when_titles_share_one_row_at_a_wide_width():
    stdscr = _FakeStdscr(size=(24, 200))
    state = home_model.State(plugin_dir="/plugins/coxswain", leader_liveness="live", other_holder=None)
    statuses = {"leader": "fresh", "runs": "fresh", "backlog": "stale", "window": "fresh"}

    draw(stdscr, _facts_fixture(), state, statuses, _PAIRS)

    texts = [call[2] for call in stdscr.addnstr_calls]
    assert any(text == "Leader" for text in texts)
    assert any(text == "! Backlog" for text in texts)
    assert not any("! Leader" in text for text in texts)


def test_t_key_runs_claude_with_the_plugin_dir_and_opening():
    state = home_model.State(plugin_dir="/plugins/coxswain", leader_liveness="none", other_holder=None)
    _, effect = home_model.step(state, "t")
    calls = []

    stopped = run_effect(effect, runner=calls.append)

    assert calls == [["claude", "--plugin-dir", "/plugins/coxswain", home_model.OPENING_CONTEXT]]
    assert stopped is False


def test_s_key_runs_cox_setup():
    state = home_model.State(plugin_dir="", leader_liveness="none", other_holder=None)
    _, effect = home_model.step(state, "s")
    calls = []

    stopped = run_effect(effect, runner=calls.append)

    assert calls == [list(home_model.SETUP_ARGV)]
    assert stopped is False


def test_q_key_stops_the_loop_with_no_subprocess_call():
    state = home_model.State(plugin_dir="", leader_liveness="none", other_holder=None)
    _, effect = home_model.step(state, "q")
    calls = []

    stopped = run_effect(effect, runner=calls.append)

    assert calls == []
    assert stopped is True


# -- chat: key translation, sending, and the panel it feeds ------------------------


def test_key_for_names_enter_and_esc_and_passes_any_other_byte_through():
    assert _key_for(10) == "ENTER"
    assert _key_for(27) == "ESC"
    assert _key_for(ord("l")) == "l"


def test_send_chat_appends_an_operator_line_readable_back(tmp_path):
    runs_dir = tmp_path / "runs"

    _send_chat(runs_dir, "hello")

    thread = leader_chat.read_thread(leader_chat.chat_path(runs_dir).read_text(encoding="utf-8"), limit=10)
    assert thread == [{"at": thread[0]["at"], "from": "operator", "text": "hello"}]


def test_facts_reads_the_chat_thread_from_the_runs_dir(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    _send_chat(runs_dir, "hello")

    result, cache = facts(runs_dir, work_dir, intake_dir, now=1_700_000_000.0)

    assert result.chat == ({"at": result.chat[0]["at"], "from": "operator", "text": "hello"},)
    assert cache["_status"]["chat"] == "fresh"


def test_draw_renders_the_chat_header_and_the_in_progress_draft():
    stdscr = _FakeStdscr()
    state = home_model.State(plugin_dir="/p", leader_liveness="live", other_holder=None, chat_draft="typing")

    draw(stdscr, _facts_fixture(), state, {}, _PAIRS)

    text = [call[2] for call in stdscr.addnstr_calls]
    assert any("CHAT" in line for line in text)
    assert any(line == "> typing" for line in text)


def test_paint_advances_the_column_cursor_across_two_spans_on_one_row():
    stdscr = _FakeStdscr()
    line = (Span("ab", "title"), Span("cde", "plain"))

    _paint(stdscr, 3, line, 80, _PAIRS)

    assert stdscr.addnstr_calls[0][:3] == (3, 0, "ab")
    assert stdscr.addnstr_calls[1][:3] == (3, 2, "cde")


def test_paint_gives_an_unknown_role_the_plain_attribute():
    stdscr = _FakeStdscr()
    line = (Span("x", "mystery"),)

    _paint(stdscr, 0, line, 80, {"plain": 5})

    assert stdscr.addnstr_calls[0][4] == 5


def test_paint_does_not_write_past_the_terminal_edge():
    stdscr = _FakeStdscr()
    line = (Span("0123456789", "plain"),)

    _paint(stdscr, 0, line, 5, _PAIRS)

    assert all(call[1] + call[3] <= 5 for call in stdscr.addnstr_calls)


def test_typing_l_into_a_focused_chat_extends_the_draft_and_does_not_land():
    """The action keys must not fire while chat holds focus: routing raw curses byte
    codes for 'c' then 'l' through `_key_for` and `home_model.step` must extend the
    draft, never arm a land."""
    state = home_model.State(
        plugin_dir="/p", leader_liveness="none", other_holder=None,
        selected_run="r1", selected_status="exited",
    )
    state, _ = home_model.step(state, _key_for(ord("c")))
    state, effect = home_model.step(state, _key_for(ord("l")))
    assert state.chat_draft == "l"
    assert state.land_armed is None
    assert effect is None


def test_a_malformed_or_missing_layout_file_yields_the_default(tmp_path):
    bad = tmp_path / "home.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_layout(bad) == home_layout.DEFAULT
    assert load_layout(tmp_path / "absent.json") == home_layout.DEFAULT


def test_a_saved_layout_survives_a_restart(tmp_path):
    moved = home_layout.step(home_layout.DEFAULT, "3")
    save_layout(tmp_path / "cox" / "home.json", moved)
    assert load_layout(tmp_path / "cox" / "home.json") == moved
