import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agent_tools import cli, home_screen
from agent_tools.pacing import Window, assess
from agent_tools.usage_window import (
    DEFAULT_POLICY,
    WeeklyReset,
    _read_usage_files,
    _usage_started,
    block_remaining,
    ceiling_remaining,
    gather,
    gather_weekly,
    parse_weekly_reset,
    read_usage,
    usage_cost_usd,
    weekly_window_from,
    weekly_window_start,
    window_from,
)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _active_block(cost=30.0, active=True, **overrides):
    block = {
        "isActive": active,
        "startTime": (_NOW.replace(hour=_NOW.hour - 1)).isoformat(),
        "endTime": (_NOW.replace(hour=_NOW.hour + 3)).isoformat(),
        "costUSD": cost,
    }
    block.update(overrides)
    return block


def test_window_from_uses_the_active_blocks_own_spend_and_burn():
    blocks_json = {"blocks": [_active_block()]}
    window = window_from(blocks_json, [], _NOW)
    assert window.spent_usd == 30.0
    assert window.burn_usd_per_hour == 30.0 / 4  # 4-hour block span
    assert window.ceiling_usd is None


def test_empty_blocks_list_falls_back_to_usage_files():
    inside = _NOW.replace(hour=11)
    window = window_from({"blocks": []}, [(inside, {"cost_usd": 5.0})], _NOW, window_hours=5.0)
    assert window.spent_usd == 5.0
    assert window.start == _NOW - (window.end - window.start)


def test_a_block_present_but_not_marked_active_falls_back_to_usage_files():
    inside = _NOW.replace(hour=11)
    blocks_json = {"blocks": [_active_block(active=False)]}
    window = window_from(blocks_json, [(inside, {"cost_usd": 7.0})], _NOW, window_hours=5.0)
    assert window.spent_usd == 7.0


def test_a_usage_file_before_the_window_is_excluded():
    inside = _NOW.replace(hour=11)
    before = _NOW.replace(day=_NOW.day - 1)
    window = window_from(
        {"blocks": []},
        [(inside, {"cost_usd": 5.0}), (before, {"cost_usd": 100.0})],
        _NOW,
        window_hours=5.0,
    )
    assert window.spent_usd == 5.0
    assert window.runs_in_flight == 1


def test_a_garbage_block_is_ignored_not_raised():
    inside = _NOW.replace(hour=11)
    blocks_json = {"blocks": [{"isActive": True, "startTime": "not-a-date", "costUSD": 30.0, "endTime": "also-not"}]}
    window = window_from(blocks_json, [(inside, {"cost_usd": 5.0})], _NOW, window_hours=5.0)
    assert window.spent_usd == 5.0


def test_a_block_missing_costusd_is_ignored_not_raised():
    inside = _NOW.replace(hour=11)
    blocks_json = {"blocks": [_active_block(cost="not-a-number")]}
    window = window_from(blocks_json, [(inside, {"cost_usd": 5.0})], _NOW, window_hours=5.0)
    assert window.spent_usd == 5.0


def test_fallback_window_spans_now_minus_window_hours_to_now():
    window = window_from({"blocks": []}, [], _NOW, window_hours=5.0)
    assert window.end == _NOW
    assert (window.end - window.start).total_seconds() / 3600 == 5.0


class _FakeResult:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_gather_uses_ccusage_when_it_answers_with_an_active_block():
    def fake_run(argv, **kwargs):
        assert argv[:3] == ["npx", "-y", "ccusage@latest"]
        return _FakeResult(0, f'{{"blocks": [{_block_json()}]}}')

    window = gather(runs_dir="/does/not/exist", now=_NOW, run=fake_run)
    assert window.spent_usd == 30.0


def _block_json():
    import json
    return json.dumps(_active_block())


def test_gather_falls_back_to_usage_files_when_the_launch_fails(tmp_path):
    path = tmp_path / "one.usage.json"
    path.write_text('{"cost_usd": 4.5}', encoding="utf-8")
    mtime = _NOW.replace(hour=11).timestamp()
    os.utime(path, (mtime, mtime))

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("npx not found")

    window = gather(runs_dir=tmp_path, now=_NOW, run=fake_run, window_hours=5.0)
    assert window.spent_usd == 4.5


def test_gather_falls_back_when_ccusage_returns_no_blocks(tmp_path):
    path = tmp_path / "one.usage.json"
    path.write_text('{"cost_usd": 2.0}', encoding="utf-8")
    mtime = _NOW.replace(hour=11).timestamp()
    os.utime(path, (mtime, mtime))

    def fake_run(argv, **kwargs):
        return _FakeResult(0, '{"blocks": []}')

    window = gather(runs_dir=tmp_path, now=_NOW, run=fake_run, window_hours=5.0)
    assert window.spent_usd == 2.0


def _counting_run(*outcomes):
    """A fake `run` answering with each outcome in turn, plus its call log. An
    exception outcome is raised; anything else is returned."""
    calls = []

    def fake_run(argv, **kwargs):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(argv)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake_run, calls


def _answer():
    return _FakeResult(0, f'{{"blocks": [{_block_json()}]}}')


def test_two_gathers_thirty_seconds_apart_run_ccusage_once(tmp_path):
    fake_run, calls = _counting_run(_answer())
    first = gather(runs_dir=tmp_path, now=_NOW, run=fake_run)
    second = gather(runs_dir=tmp_path, now=_NOW + timedelta(seconds=30), run=fake_run)
    assert (len(calls), first.spent_usd, second.spent_usd) == (1, 30.0, 30.0)


def test_two_gathers_ninety_seconds_apart_run_ccusage_twice(tmp_path):
    fake_run, calls = _counting_run(_answer())
    gather(runs_dir=tmp_path, now=_NOW, run=fake_run)
    gather(runs_dir=tmp_path, now=_NOW + timedelta(seconds=90), run=fake_run)
    assert len(calls) == 2


def test_a_failed_ccusage_is_not_cached(tmp_path):
    import subprocess
    for failure in (_FakeResult(1, ""), subprocess.TimeoutExpired("npx", 30)):
        fake_run, calls = _counting_run(failure, _answer())
        gather(runs_dir=tmp_path, now=_NOW, run=fake_run)
        assert not (tmp_path / ".ccusage-block.json").exists()
        window = gather(runs_dir=tmp_path, now=_NOW + timedelta(seconds=1), run=fake_run)
        assert (len(calls), window.spent_usd) == (2, 30.0)
        (tmp_path / ".ccusage-block.json").unlink()


def test_a_malformed_cache_file_is_ignored(tmp_path):
    (tmp_path / ".ccusage-block.json").write_text("not json", encoding="utf-8")
    fake_run, calls = _counting_run(_answer())
    window = gather(runs_dir=tmp_path, now=_NOW, run=fake_run)
    assert (len(calls), window.spent_usd) == (1, 30.0)


def test_ccusage_path_assessment_traces_back_to_the_blocks_own_numbers():
    window = window_from({"blocks": [_active_block()]}, [], _NOW)
    result = assess(window, DEFAULT_POLICY, _NOW)
    assert result.verdict == "go"
    assert "unmeasured" in result.reason
    hours_remaining = (window.end - _NOW).total_seconds() / 3600
    assert result.projected_total == window.spent_usd + window.burn_usd_per_hour * hours_remaining


def test_fallback_path_assessment_traces_back_to_the_in_window_sum_and_burn():
    inside = _NOW.replace(hour=11)
    window = window_from({"blocks": []}, [(inside, {"cost_usd": 12.0})], _NOW, window_hours=5.0)
    result = assess(window, DEFAULT_POLICY, _NOW)
    assert window.spent_usd == 12.0
    assert window.burn_usd_per_hour == 12.0 / 5.0
    assert result.projected_total == window.spent_usd  # now == window.end, no hours remaining


def test_default_policy_never_tightens_an_absent_pacing_policy():
    window = window_from({"blocks": []}, [], _NOW, window_hours=5.0)
    result = assess(window, DEFAULT_POLICY, _NOW)
    assert result.tier_ceiling == "deep"
    assert result.effort_ceiling == "high"


def test_a_passed_ceiling_reaches_the_window_on_the_ccusage_path():
    window = window_from({"blocks": [_active_block()]}, [], _NOW, ceiling_usd=50.0)
    assert window.ceiling_usd == 50.0


def test_a_passed_ceiling_reaches_the_window_on_the_fallback_path():
    inside = _NOW.replace(hour=11)
    window = window_from({"blocks": []}, [(inside, {"cost_usd": 5.0})], _NOW, ceiling_usd=50.0)
    assert window.ceiling_usd == 50.0


def test_gather_threads_a_passed_ceiling_onto_the_window(tmp_path):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("npx not found")

    window = gather(runs_dir=tmp_path, now=_NOW, run=fake_run, ceiling_usd=3.0)
    assert window.ceiling_usd == 3.0


def _window(ceiling_usd=None, spent_usd=0.0, start=_NOW, end=_NOW):
    return Window(start=start, end=end, spent_usd=spent_usd, ceiling_usd=ceiling_usd, burn_usd_per_hour=0.0, runs_in_flight=0)


def test_block_remaining_at_half_elapsed_is_half_the_fraction():
    window = _window(start=_NOW, end=_NOW.replace(hour=_NOW.hour + 4))
    _, fraction = block_remaining(window, _NOW.replace(hour=_NOW.hour + 2))
    assert fraction == 0.5


def test_block_remaining_with_now_past_end_is_zero_not_negative():
    window = _window(start=_NOW.replace(hour=_NOW.hour - 2), end=_NOW.replace(hour=_NOW.hour - 1))
    minutes, fraction = block_remaining(window, _NOW)
    assert minutes == 0
    assert fraction == 0.0


def test_ceiling_remaining_clamps_a_spend_over_the_ceiling_to_zero():
    window = _window(ceiling_usd=10.0, spent_usd=15.0)
    assert ceiling_remaining(window) == 0.0


def test_ceiling_remaining_is_none_with_no_ceiling_set():
    window = _window(ceiling_usd=None, spent_usd=15.0)
    assert ceiling_remaining(window) is None


def _capturing_gather(captured):
    def fake_gather(runs_dir, now, ceiling_usd=None, usage=None):
        captured["ceiling_usd"] = ceiling_usd
        return window_from({"blocks": []}, [], now, window_hours=5.0)
    return fake_gather


def test_cli_usage_assessment_threads_the_profile_ceiling_into_gather(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.usage_window, "gather", _capturing_gather(captured))
    cli._usage_assessment(tmp_path, window_ceiling_usd=250.0)
    assert captured["ceiling_usd"] == 250.0


def test_cli_usage_assessment_passes_none_when_the_profile_has_no_ceiling(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli.usage_window, "gather", _capturing_gather(captured))
    cli._usage_assessment(tmp_path)
    assert captured["ceiling_usd"] is None


def test_home_screen_read_window_threads_the_profile_ceiling_into_gather(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(home_screen.usage_window, "gather", _capturing_gather(captured))
    home_screen._read_window(tmp_path, _NOW, window_ceiling_usd=125.0)
    assert captured["ceiling_usd"] == 125.0


def test_home_screen_read_window_passes_none_when_the_profile_has_no_ceiling(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(home_screen.usage_window, "gather", _capturing_gather(captured))
    home_screen._read_window(tmp_path, _NOW)
    assert captured["ceiling_usd"] is None


def test_weekly_window_from_includes_a_usage_file_three_days_old(tmp_path):
    inside = _NOW - timedelta(days=3)
    window = weekly_window_from([(inside, {"cost_usd": 9.0})], _NOW)
    assert window.spent_usd == 9.0
    assert window.start == _NOW - timedelta(days=7)


def test_weekly_window_from_excludes_a_usage_file_eight_days_old(tmp_path):
    outside = _NOW - timedelta(days=8)
    window = weekly_window_from([(outside, {"cost_usd": 9.0})], _NOW)
    assert window.spent_usd == 0.0


def test_weekly_window_from_with_reset_anchors_start_at_weekly_window_start():
    reset = WeeklyReset(weekday=_NOW.weekday(), hour=4, minute=0, tz="UTC")
    window = weekly_window_from([(_NOW - timedelta(hours=1), {"cost_usd": 9.0})], _NOW, reset=reset)
    assert window.start == weekly_window_start(_NOW, reset)
    assert window.start != _NOW - timedelta(days=7)


def test_gather_weekly_threads_a_passed_ceiling_onto_the_window(tmp_path):
    path = tmp_path / "one.usage.json"
    path.write_text('{"cost_usd": 4.0}', encoding="utf-8")
    mtime = (_NOW - timedelta(days=1)).timestamp()
    os.utime(path, (mtime, mtime))
    window = gather_weekly(tmp_path, _NOW, weekly_ceiling_usd=40.0)
    assert window.spent_usd == 4.0
    assert window.ceiling_usd == 40.0


def test_usage_cost_usd_reads_the_current_shapes_summary_field():
    assert usage_cost_usd({"run_id": "r1", "calls": [], "summary": {"cost_usd": 4.5, "calls": 2}}) == 4.5


def test_usage_started_reads_started_at_nested_under_summary():
    mtime = _NOW - timedelta(days=30)
    started = _usage_started({"summary": {"started_at": _NOW.isoformat()}}, mtime)
    assert started == _NOW


def test_weekly_window_from_counts_a_current_shape_usage_file():
    inside = _NOW - timedelta(days=3)
    usage = {"run_id": "r1", "calls": [], "summary": {"cost_usd": 9.0, "calls": 2}}
    window = weekly_window_from([(inside, usage)], _NOW)
    assert window.spent_usd == 9.0


def test_weekly_window_from_still_counts_an_old_shape_file():
    inside = _NOW - timedelta(days=3)
    window = weekly_window_from([(inside, {"cost_usd": 9.0})], _NOW)
    assert window.spent_usd == 9.0


def test_gather_weekly_reports_nonzero_for_a_real_shaped_usage_file(tmp_path):
    path = tmp_path / "one.usage.json"
    path.write_text(
        json.dumps({"run_id": "r1", "calls": [], "summary": {"cost_usd": 770.0, "calls": 2}}),
        encoding="utf-8",
    )
    mtime = (_NOW - timedelta(days=1)).timestamp()
    os.utime(path, (mtime, mtime))
    window = gather_weekly(tmp_path, _NOW, weekly_ceiling_usd=1043.0)
    assert window.spent_usd == 770.0
    assert ceiling_remaining(window) != 1.0


def test_read_usage_files_starts_a_store_only_run_at_its_launched_at_and_skips_one_without(tmp_path):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, launched_at TEXT, ended_at TEXT)")
    conn.execute(
        "CREATE TABLE node_calls (call_id TEXT, run_id TEXT, seq INTEGER, task_id TEXT, cost_usd REAL, ts TEXT, "
        "model_alias TEXT, ok BOOL, decision_json TEXT, role TEXT, tier TEXT, ceiling_usd REAL, ceiling_source TEXT, "
        "turns INTEGER, duration_ms INTEGER, input_tokens INTEGER, cache_read_tokens INTEGER, "
        "cache_creation_tokens INTEGER, input_total INTEGER, output_tokens INTEGER)"
    )
    conn.executemany("INSERT INTO runs VALUES (?, ?, '2026-09-05T11:30:00+00:00')",
                     [("dated", "2026-09-05T10:15:00+00:00"), ("undated", None)])
    conn.executemany(
        "INSERT INTO node_calls (call_id, run_id, seq, cost_usd, ts, ok) VALUES (?, ?, 1, 2.5, '2026-09-05T10:20:00+00:00', 1)",
        [("c1", "dated"), ("c2", "undated")],
    )
    conn.commit()
    conn.close()
    (tmp_path / "filed.usage.json").write_text('{"cost_usd": 1.0}', encoding="utf-8")
    by_cost = {usage_cost_usd(usage): started for started, usage in _read_usage_files(tmp_path, _NOW)}
    assert sorted(by_cost) == [1.0, 2.5]
    assert by_cost[2.5] == datetime(2026, 9, 5, 10, 15, tzinfo=UTC)


def _aged_usage_file(runs_dir, name, cost, age):
    path = runs_dir / f"{name}.usage.json"
    path.write_text(json.dumps({"summary": {"cost_usd": cost}}), encoding="utf-8")
    mtime = (_NOW - age).timestamp()
    os.utime(path, (mtime, mtime))


def test_a_usage_file_older_than_seven_days_is_not_opened_and_counts_in_neither_window(tmp_path, monkeypatch):
    _aged_usage_file(tmp_path, "old", 50.0, timedelta(days=8))
    _aged_usage_file(tmp_path, "new", 4.0, timedelta(hours=1))
    opened = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda text, *a, **k: opened.append(text) or real(text, *a, **k))
    usage = read_usage(tmp_path, _NOW)
    assert [u["summary"]["cost_usd"] for _, u in usage] == [4.0]
    assert len(opened) == 1
    assert gather_weekly(tmp_path, _NOW).spent_usd == 4.0
    assert window_from({"blocks": []}, usage, _NOW, window_hours=5.0).spent_usd == 4.0


def test_a_skipped_old_file_does_not_let_the_store_answer_for_its_run(tmp_path):
    _aged_usage_file(tmp_path, "r1", 50.0, timedelta(days=8))
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, launched_at TEXT, ended_at TEXT)")
    conn.execute("CREATE TABLE node_calls (call_id TEXT, run_id TEXT, seq INTEGER, cost_usd REAL, ts TEXT, ok BOOL)")
    conn.execute("INSERT INTO runs VALUES ('r1', ?, NULL)", ((_NOW - timedelta(hours=1)).isoformat(),))
    conn.execute("INSERT INTO node_calls VALUES ('c1', 'r1', 1, 9.0, ?, 1)", (_NOW.isoformat(),))
    conn.commit()
    conn.close()
    assert read_usage(tmp_path, _NOW) == []


def _no_read(*_a, **_k):
    raise AssertionError("read the runs dir")


def test_gather_and_gather_weekly_use_a_given_usage_instead_of_reading(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_tools.usage_window.read_usage", _no_read)
    given = [(_NOW - timedelta(hours=1), {"cost_usd": 3.0})]
    assert gather_weekly(tmp_path, _NOW, usage=given).spent_usd == 3.0
    no_ccusage = lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "")  # noqa: E731
    assert gather(tmp_path, _NOW, run=no_ccusage, usage=given).spent_usd == 3.0


def test_usage_assessment_reads_the_runs_dir_once(tmp_path, monkeypatch):
    calls = []
    real = read_usage
    monkeypatch.setattr(cli.usage_window, "read_usage", lambda d, now, reset=None: calls.append(d) or real(d, now, reset))
    cli._usage_assessment(tmp_path)
    assert calls == [tmp_path]


def test_gather_weekly_takes_the_store_spend_over_usage_files_that_sum_differently(tmp_path):
    _aged_usage_file(tmp_path, "r1", 4.0, timedelta(hours=1))
    window = gather_weekly(tmp_path, _NOW, store_spend=lambda since: 123.0)
    assert (window.spent_usd, window.runs_in_flight) == (123.0, 0)


def test_gather_weekly_falls_back_to_the_usage_files_when_the_store_gives_none(tmp_path):
    _aged_usage_file(tmp_path, "r1", 4.0, timedelta(hours=1))
    assert gather_weekly(tmp_path, _NOW, store_spend=lambda since: None).spent_usd == 4.0


def test_gather_weekly_asks_the_store_from_seven_days_before_now_as_iso_text(tmp_path):
    seen = []
    gather_weekly(tmp_path, _NOW, store_spend=lambda since: seen.append(since) or 1.0)
    assert seen == ["2026-08-29T12:00:00+00:00"]


def test_gather_weekly_with_reset_anchors_start_through_the_store_path(tmp_path):
    reset = WeeklyReset(weekday=_NOW.weekday(), hour=4, minute=0, tz="UTC")
    window = gather_weekly(tmp_path, _NOW, store_spend=lambda since: 123.0, reset=reset)
    assert window.start == weekly_window_start(_NOW, reset)


def test_gather_weekly_with_reset_anchors_start_through_the_usage_files_path(tmp_path):
    reset = WeeklyReset(weekday=_NOW.weekday(), hour=4, minute=0, tz="UTC")
    given = [(_NOW - timedelta(hours=1), {"cost_usd": 3.0})]
    window = gather_weekly(tmp_path, _NOW, usage=given, reset=reset)
    assert window.start == weekly_window_start(_NOW, reset)


def test_weekly_window_start_with_no_reset_is_the_rolling_7_day_start():
    assert weekly_window_start(_NOW, None) == _NOW - timedelta(days=7)


def test_weekly_window_start_reset_earlier_today_starts_today():
    # _NOW is Saturday 2026-09-05 12:00 UTC; a Saturday 04:00 reset already
    # passed today, so the window starts at today's occurrence.
    reset = WeeklyReset(weekday=_NOW.weekday(), hour=4, minute=0, tz="UTC")
    assert weekly_window_start(_NOW, reset) == datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


def test_weekly_window_start_reset_later_today_starts_last_week():
    # A Saturday 18:00 reset has not happened yet today, so the most recent
    # occurrence is last Saturday, not today.
    reset = WeeklyReset(weekday=_NOW.weekday(), hour=18, minute=0, tz="UTC")
    assert weekly_window_start(_NOW, reset) == datetime(2026, 8, 29, 18, 0, tzinfo=UTC)


def test_weekly_window_start_holds_wall_clock_hour_across_dst():
    # US clocks fall back Sunday 2026-11-01: Saturday 2026-10-31 23:00 is
    # still EDT (UTC-4). A naive fixed-offset computation using `now`'s own
    # EST (UTC-5) would land an hour off, at 04:00 UTC instead of 03:00.
    reset = WeeklyReset(weekday=5, hour=23, minute=0, tz="America/New_York")
    now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # Monday, after the fall back
    start = weekly_window_start(now, reset)
    assert start == datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    local_start = start.astimezone(ZoneInfo("America/New_York"))
    assert (local_start.weekday(), local_start.hour, local_start.minute) == (5, 23, 0)


def test_parse_weekly_reset_parses_dow_time_zone():
    assert parse_weekly_reset("Sun 04:00 America/New_York") == WeeklyReset(
        weekday=6, hour=4, minute=0, tz="America/New_York",
    )


def test_parse_weekly_reset_returns_none_for_absent_or_bad_input():
    assert parse_weekly_reset(None) is None
    assert parse_weekly_reset("not a reset") is None
