import datetime
import json
import os
import socket
import sqlite3
import time
from dataclasses import replace

import pytest
from test_run_store import lane_run, lane_store, leases_table

from agent_tools import cli, run_store, runs_top
from agent_tools.runs_top_screen import _fact, calls_from_usage, draw, facts, first_visible, loop, rows_now

_WRITTEN = datetime.datetime(2026, 9, 25, 5, 29, 0, tzinfo=datetime.UTC).timestamp()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def test_facts_reads_one_alive_run_with_calls_and_phases(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r1:build.json", "{}")
    trace = tmp_path / "r1-trace"
    trace.mkdir()
    _write(trace / "n1-1.jsonl", json.dumps({"type": "result", "num_turns": 4, "total_cost_usd": 0.5}) + "\n")
    _write(trace / "n2-1.jsonl", json.dumps({"type": "assistant"}) + "\n")
    os.utime(trace / "n1-1.jsonl", (_WRITTEN, _WRITTEN))

    result = facts(tmp_path, now_alive=lambda pid: pid == 123)

    assert len(result) == 1
    f = result[0]
    assert f["run"] == "r1"
    assert f["alive"] is True
    assert f["phases"] == ["build"]
    assert f["calls"] == [{"node": "n1", "attempt": 1, "cost_usd": 0.5, "turns": 4, "ts": "2026-09-25T05:29:00Z"}]


def test_calls_from_usage_numbers_attempts_per_role_in_order():
    stored = [
        {"role": "plan", "cost_usd": 0.1, "turns": 2},
        {"role": "build", "cost_usd": 0.2, "turns": 3},
        {"role": "build", "cost_usd": 0.3, "turns": 4},
    ]

    assert calls_from_usage(stored) == [
        {"node": "plan", "attempt": 1, "cost_usd": 0.1, "turns": 2, "ts": None},
        {"node": "build", "attempt": 1, "cost_usd": 0.2, "turns": 3, "ts": None},
        {"node": "build", "attempt": 2, "cost_usd": 0.3, "turns": 4, "ts": None},
    ]


def test_fact_with_no_trace_dir_reads_calls_from_the_store(tmp_path, monkeypatch):
    stored = [{"role": "plan", "cost_usd": 0.1, "turns": 2}, {"role": "build", "cost_usd": 0.2, "turns": 3}]
    monkeypatch.setattr(run_store, "usage", lambda root, run: {"calls": stored})

    fact = _fact(tmp_path, "r1", False)

    assert fact["calls"] == calls_from_usage(stored)
    started = [e.detail["node"] for e in fact["events"] if e.kind == "node_started"]
    assert started == ["plan", "build"]


def test_fact_with_trace_files_ignores_the_store(tmp_path, monkeypatch):
    def boom(root, run):
        raise AssertionError("the store must not be read when trace files exist")

    monkeypatch.setattr(run_store, "usage", boom)
    trace = tmp_path / "r1-trace"
    trace.mkdir()
    _write(trace / "n1-1.jsonl", json.dumps({"type": "result", "num_turns": 4, "total_cost_usd": 0.5}) + "\n")
    os.utime(trace / "n1-1.jsonl", (_WRITTEN, _WRITTEN))

    fact = _fact(tmp_path, "r1", True)

    assert fact["calls"] == [{"node": "n1", "attempt": 1, "cost_usd": 0.5, "turns": 4, "ts": "2026-09-25T05:29:00Z"}]


def test_facts_lists_store_phases_by_ts_for_a_live_run_with_no_phase_files(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE phases (run_id TEXT, phase_id TEXT, ts TEXT, record_json TEXT)")
    conn.executemany(
        "INSERT INTO phases (run_id, phase_id, ts) VALUES (?, ?, ?)",
        [("r1", "a-late", "2026-09-25T04:50:00+00:00"), ("r1", "z-early", "2026-09-25T04:40:00+00:00")],
    )
    conn.commit()
    conn.close()

    result = facts(tmp_path, now_alive=lambda pid: pid == 123)

    assert result[0]["phases"] == ["z-early", "a-late"]


def test_rows_now_maps_facts_through_runs_top_row(tmp_path):
    assert rows_now(tmp_path) == []


def test_facts_reads_the_runs_ceiling_file(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r1.ceiling.json", json.dumps(
        {"requested": {"tier": "standard", "effort": None}, "applied": {"tier": "standard", "effort": "high"}, "profile": "p.yaml"}))

    result = facts(tmp_path, now_alive=lambda pid: pid == 123)

    assert result[0]["ceiling"]["applied"] == {"tier": "standard", "effort": "high"}


def test_facts_ceiling_is_none_with_no_ceiling_file(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")

    result = facts(tmp_path, now_alive=lambda pid: pid == 123)

    assert result[0]["ceiling"] is None


def test_rows_now_carries_the_ceiling_label_into_the_row(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r1.ceiling.json", json.dumps(
        {"requested": {"tier": "standard", "effort": None}, "applied": {"tier": "standard", "effort": "high"}, "profile": "p.yaml"}))

    rows = rows_now(tmp_path)

    assert rows[0].ceiling == "standard/high"


def test_facts_omits_a_dead_run_with_a_stale_log(tmp_path):
    _write(tmp_path / "r2.pid", "999")
    log = tmp_path / "r2.log"
    _write(log, "old\n")
    old = time.time() - 3600
    import os
    os.utime(log, (old, old))

    result = facts(tmp_path, now_alive=lambda pid: False)

    assert result == []


class _FakeStdscr:
    def __init__(self, keys, size=(24, 80)):
        self._keys = list(keys)
        self._size = size
        self.draws = 0
        self.addnstr_calls = []
        self.checkpoints = []

    def getmaxyx(self):
        return self._size

    def clear(self):
        self.draws += 1

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def addnstr(self, y, x, s, n, attr=0):
        self.addnstr_calls.append((y, x, s, n, attr))

    def getch(self):
        self.checkpoints.append(len(self.addnstr_calls))
        return self._keys.pop(0)


def test_draw_shows_the_short_id_on_the_run_line_and_the_key_when_there_is_none():
    keyed = runs_top.row("acme-20261005-long-slug", True, [], [], [], None)
    stdscr = _FakeStdscr([], size=(24, 200))
    draw(stdscr, [replace(keyed, short_id="I7-2")])
    shown = " ".join(c[2] for c in stdscr.addnstr_calls)
    assert "I7-2" in shown
    assert "acme-20261005-long-slug" not in shown

    stdscr = _FakeStdscr([], size=(24, 200))
    draw(stdscr, [keyed])
    assert any("acme-20261005-long-slug" in c[2] for c in stdscr.addnstr_calls)


def test_draw_never_writes_a_line_wider_than_the_fake_width():
    stdscr = _FakeStdscr([], size=(24, 10))
    draw(stdscr, [])
    assert stdscr.addnstr_calls
    assert all(len(call[2]) <= call[3] for call in stdscr.addnstr_calls)


def test_loop_returns_0_on_an_immediate_q():
    stdscr = _FakeStdscr([ord("q")])
    rc = loop(stdscr, "unused", 1, tick=lambda d: [])
    assert rc == 0
    assert stdscr.draws == 1


def test_loop_redraws_once_on_a_resize_then_exits_on_q():
    curses = pytest.importorskip("curses")
    stdscr = _FakeStdscr([curses.KEY_RESIZE, ord("q")])
    rc = loop(stdscr, "unused", 1, tick=lambda d: [])
    assert rc == 0
    assert stdscr.draws == 2


def test_cli_runs_top_once_prints_the_header(tmp_path, capsys):
    rc = cli.main(["runs", "top", "--runs-dir", str(tmp_path), "--once"])
    assert rc == 0
    assert "RUN" in capsys.readouterr().out


def _terminals(monkeypatch, coxtop: bool, tty: bool) -> list:
    """Stdin and stdout report `tty`; `which` finds coxtop when asked; `os.execv` is recorded, not run."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    monkeypatch.setattr("sys.stdout.isatty", lambda: tty)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/coxtop" if coxtop else None)
    calls = []
    monkeypatch.setattr("os.execv", lambda path, argv: calls.append((path, argv)))
    return calls


def test_cli_runs_top_on_a_terminal_with_coxtop_execs_it_and_prints_no_table(tmp_path, monkeypatch, capsys):
    calls = _terminals(monkeypatch, coxtop=True, tty=True)

    rc = cli.main(["runs", "top", "--runs-dir", str(tmp_path)])

    assert rc == 0
    assert calls == [("/usr/bin/coxtop", ["/usr/bin/coxtop"])]
    assert "RUN" not in capsys.readouterr().out


def test_cli_runs_top_without_a_terminal_prints_what_once_prints(tmp_path, monkeypatch, capsys):
    calls = _terminals(monkeypatch, coxtop=True, tty=False)
    argv = ["runs", "top", "--runs-dir", str(tmp_path)]

    assert cli.main(argv) == 0
    plain = capsys.readouterr().out
    assert cli.main([*argv, "--once"]) == 0

    assert plain == capsys.readouterr().out
    assert plain.splitlines()[-1] == "all lanes clear"
    assert calls == []


def test_cli_runs_top_once_never_execs_coxtop_even_on_a_terminal(tmp_path, monkeypatch, capsys):
    calls = _terminals(monkeypatch, coxtop=True, tty=True)

    rc = cli.main(["runs", "top", "--runs-dir", str(tmp_path), "--once"])

    assert rc == 0
    assert calls == []
    assert "RUN" in capsys.readouterr().out


def test_loop_enter_expands_the_row_in_place_and_enter_again_collapses_it(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    stdscr = _FakeStdscr([ord("\n"), ord("\n"), ord("q")])

    rc = loop(stdscr, tmp_path, 1, tick=rows_now, now_alive=lambda pid: True)

    bounds = [0, *stdscr.checkpoints]
    draws = [stdscr.addnstr_calls[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    assert rc == 0
    assert not any(c[2].startswith("  ") for c in draws[0])
    assert any(c[2].startswith("  ") for c in draws[1])
    assert not any(c[2].startswith("  ") for c in draws[2])


def test_draw_marks_the_cursor_row_with_a_reverse_attr(tmp_path):
    curses = pytest.importorskip("curses")
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    stdscr = _FakeStdscr([])

    draw(stdscr, rows_now(tmp_path), cursor=0)

    row_call = next(c for c in stdscr.addnstr_calls if c[0] == 1)
    assert row_call[4] & curses.A_REVERSE


def test_loop_j_moves_the_cursor_so_enter_expands_the_second_row(tmp_path):
    from agent_tools import runs_top

    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r2.pid", "124")
    _write(tmp_path / "r2.log", "n1 verdict: land\n")
    rows = [runs_top.row("r1", True, [], [], [], None), runs_top.row("r2", True, [], [], [], None)]
    stdscr = _FakeStdscr([ord("j"), ord("\n"), ord("q")])

    rc = loop(stdscr, tmp_path, 1, tick=lambda d: rows, now_alive=lambda pid: True)

    assert rc == 0
    assert any("run r2 [" in call[2] for call in stdscr.addnstr_calls)
    assert not any("run r1 [" in call[2] for call in stdscr.addnstr_calls)


def test_loop_enter_on_another_row_moves_the_expansion_there(tmp_path):
    from agent_tools import runs_top

    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r2.pid", "124")
    _write(tmp_path / "r2.log", "n1 verdict: land\n")
    rows = [runs_top.row("r1", True, [], [], [], None), runs_top.row("r2", True, [], [], [], None)]
    stdscr = _FakeStdscr([ord("\n"), ord("j"), ord("\n"), ord("q")])

    rc = loop(stdscr, tmp_path, 1, tick=lambda d: rows, now_alive=lambda pid: True)

    bounds = [0, *stdscr.checkpoints]
    draws = [stdscr.addnstr_calls[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    assert rc == 0
    assert any("run r1 [" in c[2] for c in draws[1])
    assert any("run r2 [" in c[2] for c in draws[3])
    assert not any("run r1 [" in c[2] for c in draws[3])


def test_first_visible_leaves_a_cursor_already_on_screen_alone():
    assert first_visible(cursor_index=7, total_lines=30, window_height=10, current_first=5) == 5


def test_first_visible_scrolls_the_minimum_to_bring_the_cursor_back_on_screen():
    assert first_visible(cursor_index=15, total_lines=30, window_height=10, current_first=0) == 6


def test_cli_runs_top_once_prints_ceil_for_a_run_with_a_ceiling_file(tmp_path, capsys):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r1.ceiling.json", json.dumps(
        {"requested": {"tier": "standard", "effort": None}, "applied": {"tier": "standard", "effort": "high"}, "profile": "p.yaml"}))

    rc = cli.main(["runs", "top", "--runs-dir", str(tmp_path), "--once"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CEIL" in out
    assert "standard/high" in out


def test_first_visible_keeps_the_cursor_on_screen_past_a_tall_expansion_without_a_leader_line():
    # header + row0 (expanded, 40 detail lines) + row1 at cursor: cursor_index 42 of 43 lines.
    assert first_visible(cursor_index=42, total_lines=43, window_height=10, current_first=0) == 33


def test_first_visible_keeps_the_cursor_on_screen_past_a_tall_expansion_with_a_leader_line():
    # same shape, shifted down one line by the leader line above the header.
    assert first_visible(cursor_index=43, total_lines=44, window_height=10, current_first=0) == 34


def test_loop_esc_clears_the_expansion(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    stdscr = _FakeStdscr([ord("\n"), 27, ord("q")])

    rc = loop(stdscr, tmp_path, 1, tick=rows_now, now_alive=lambda pid: True)

    bounds = [0, *stdscr.checkpoints]
    draws = [stdscr.addnstr_calls[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    assert rc == 0
    assert any(c[2].startswith("  ") for c in draws[1])
    assert not any(c[2].startswith("  ") for c in draws[2])


def test_loop_j_moves_the_cursor_without_disturbing_an_existing_expansion(tmp_path):
    from agent_tools import runs_top

    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    _write(tmp_path / "r2.pid", "124")
    _write(tmp_path / "r2.log", "n1 verdict: land\n")
    rows = [runs_top.row("r1", True, [], [], [], None), runs_top.row("r2", True, [], [], [], None)]
    stdscr = _FakeStdscr([ord("\n"), ord("j"), ord("k"), ord("q")])

    rc = loop(stdscr, tmp_path, 1, tick=lambda d: rows, now_alive=lambda pid: True)

    bounds = [0, *stdscr.checkpoints]
    draws = [stdscr.addnstr_calls[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    assert rc == 0
    assert all(any("run r1 [" in c[2] for c in draw) for draw in draws[1:])
    assert not any("run r2 [" in c[2] for c in draws[-1])


def test_the_accordion_tail_carries_message_text_not_a_repeat_of_the_tool_name(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "n1 verdict: land\n")
    trace = tmp_path / "r1-trace"
    trace.mkdir()
    events = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "finished the migration"}]}}),
    ]
    _write(trace / "n1-1.jsonl", "\n".join(events) + "\n")
    stdscr = _FakeStdscr([ord("\n"), ord("q")])

    rc = loop(stdscr, tmp_path, 1, tick=rows_now, now_alive=lambda pid: True)

    text = "".join(c[2] for c in stdscr.addnstr_calls)
    assert rc == 0
    assert "finished the migration" in text
    assert text.count("Bash") == 1


def test_the_row_names_the_node_written_last_not_the_last_one_alphabetically(tmp_path):
    _write(tmp_path / "r1.pid", "123")
    _write(tmp_path / "r1.log", "")
    trace = tmp_path / "r1-trace"
    trace.mkdir()
    result = json.dumps({"type": "result", "num_turns": 1, "total_cost_usd": 0.1}) + "\n"
    _write(trace / "scope_epic-1.jsonl", result)
    _write(trace / "build-4.jsonl", result)
    os.utime(trace / "scope_epic-1.jsonl", (1000, 1000))
    os.utime(trace / "build-4.jsonl", (2000, 2000))

    rows = rows_now(tmp_path)

    assert (rows[0].node, rows[0].attempt) == ("build", 4)


def test_facts_default_probe_reads_the_lease_not_the_pid(tmp_path):
    _write(tmp_path / "x-3.pid", str(os.getpid()))
    _write(tmp_path / "y-1.pid", "999999999")
    leases_table(tmp_path, ("runs:x", "x-4", "2026-09-25T06:00:00Z"), ("runs:y", "y-1", "2999-01-01T00:00:00Z"))

    assert {f["run"]: f["alive"] for f in facts(tmp_path)} == {"y-1": True}


_NOW = datetime.datetime(2026, 9, 25, 6, 0, 0, tzinfo=datetime.UTC)


def test_fact_heartbeat_age_is_now_minus_the_lease_heartbeat_when_this_run_holds_it(tmp_path):
    leases_table(tmp_path, ("runs:x", "x-3", "2026-09-25T06:00:00Z", "2026-09-25T05:58:30Z"))

    assert _fact(tmp_path, "x-3", True, _NOW)["heartbeat_age"] == 90


def test_fact_heartbeat_age_is_none_for_a_lease_held_by_another_run_or_no_store(tmp_path):
    assert _fact(tmp_path, "x-3", True, _NOW)["heartbeat_age"] is None
    leases_table(tmp_path, ("runs:x", "x-2", "2026-09-25T06:00:00Z", "2026-09-25T05:58:30Z"))

    assert _fact(tmp_path, "x-3", True, _NOW)["heartbeat_age"] is None


def _trace_call_written(tmp_path, minutes_before_now: int) -> None:
    """One finished trace call, `build-1.jsonl`, last written `minutes_before_now` before `_NOW`."""
    trace = tmp_path / "r1-trace"
    trace.mkdir()
    path = trace / "build-1.jsonl"
    _write(path, json.dumps({"type": "result", "num_turns": 2, "total_cost_usd": 0.1}) + "\n")
    written = (_NOW - datetime.timedelta(minutes=minutes_before_now)).timestamp()
    os.utime(path, (written, written))


def test_fact_node_call_stalled_true_when_the_newest_trace_call_is_31_minutes_old(tmp_path, monkeypatch):
    _trace_call_written(tmp_path, 31)
    monkeypatch.setattr(run_store, "run_started", lambda root, run: "2026-09-25T05:00:00Z")

    assert _fact(tmp_path, "r1", True, _NOW)["node_call_stalled"] is True


def test_fact_node_call_stalled_false_when_the_newest_trace_call_is_29_minutes_old(tmp_path, monkeypatch):
    _trace_call_written(tmp_path, 29)
    monkeypatch.setattr(run_store, "run_started", lambda root, run: "2026-09-25T05:00:00Z")

    assert _fact(tmp_path, "r1", True, _NOW)["node_call_stalled"] is False


def test_fact_node_call_stalled_reads_the_stored_call_ts_with_no_trace_files(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store, "usage", lambda root, run: {
        "calls": [{"role": "build", "cost_usd": 0.1, "turns": 2, "ts": "2026-09-25T05:31:00Z"}]})
    monkeypatch.setattr(run_store, "run_started", lambda root, run: "2026-09-25T05:00:00Z")

    assert _fact(tmp_path, "r1", True, _NOW)["node_call_stalled"] is False


_LIVE = "2026-09-25T06:02:00Z"


def _remote_store(runs_dir, beat, host_column=True):
    lane_store(runs_dir, [lane_run("x-3", "2026-09-25T05:00:00Z")], [("runs:x", "x-3", _LIVE, beat)], host_column=host_column)


def test_a_remote_lane_appears_with_remote_true_and_its_host(tmp_path):
    _remote_store(tmp_path, "2026-09-25T05:59:50Z")

    rows = rows_now(tmp_path, now=_NOW)

    assert [(r.run, r.remote, r.host, r.alive, r.status, r.heartbeat_age) for r in rows] == [("x-3", True, "h", True, "running", 10)]
    assert (rows[0].phase, rows[0].node, rows[0].turns, rows[0].cost_usd, rows[0].verdict) == ("", "starting", 0, 0.0, "")


def test_a_remote_lane_with_two_stored_calls_reports_the_second_and_the_summed_cost(tmp_path, monkeypatch):
    _remote_store(tmp_path, "2026-09-25T05:59:50Z")
    monkeypatch.setattr(run_store, "usage", lambda root, run: {"calls": [
        {"role": "plan", "cost_usd": 0.25, "turns": 2, "ts": "2026-09-25T05:10:00Z"},
        {"role": "build", "cost_usd": 0.5, "turns": 5, "ts": "2026-09-25T05:20:00Z"},
    ]})
    monkeypatch.setattr(run_store, "phase_names", lambda root, run: ["p1", "p2"])

    row = rows_now(tmp_path, now=_NOW)[0]

    assert (row.phase, row.node, row.attempt, row.turns, row.cost_usd) == ("p2", "build", 1, 7, 0.75)


def test_a_remote_lane_with_no_stored_calls_reports_starting(tmp_path, monkeypatch):
    _remote_store(tmp_path, "2026-09-25T05:59:50Z")
    monkeypatch.setattr(run_store, "usage", lambda root, run: None)
    monkeypatch.setattr(run_store, "phase_names", lambda root, run: [])

    row = rows_now(tmp_path, now=_NOW)[0]

    assert (row.phase, row.node, row.attempt, row.turns, row.cost_usd) == ("", "starting", 0, 0, 0.0)


def test_a_stalled_remote_lane_at_90_seconds_reads_stalled(tmp_path):
    _remote_store(tmp_path, "2026-09-25T05:58:30Z")

    assert [r.status for r in rows_now(tmp_path, now=_NOW)] == ["stalled"]


def test_a_remote_lane_with_no_host_column_has_host_none(tmp_path):
    _remote_store(tmp_path, "2026-09-25T05:59:50Z", host_column=False)

    assert [(r.remote, r.host) for r in rows_now(tmp_path, now=_NOW)] == [(True, None)]


def test_a_lane_whose_run_has_a_local_pidfile_appears_once_as_local(tmp_path):
    _remote_store(tmp_path, "2026-09-25T05:59:50Z")
    _write(tmp_path / "x-3.pid", str(os.getpid()))
    _write(tmp_path / "x-3.log", "")

    rows = rows_now(tmp_path, now=_NOW)

    assert [(r.run, r.remote, r.host) for r in rows] == [("x-3", False, socket.gethostname())]


def test_a_lane_on_this_machine_with_no_pidfile_shows_this_machine_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "h")
    _remote_store(tmp_path, "2026-09-25T05:59:50Z")

    assert runs_top.render(rows_now(tmp_path, now=_NOW), 200)[1].split()[:2] == ["x-3", "h"]


def test_with_no_live_leases_the_rows_equal_the_local_only_rows(tmp_path):
    _write(tmp_path / "r1.pid", str(os.getpid()))
    _write(tmp_path / "r1.log", "")
    local_only = rows_now(tmp_path, now=_NOW)
    lane_store(tmp_path, [lane_run("x-3", "2026-09-25T05:00:00Z")], [("runs:x", "x-3", "2026-09-25T05:00:00Z")], host_column=True)

    assert local_only
    assert rows_now(tmp_path, now=_NOW) == local_only
