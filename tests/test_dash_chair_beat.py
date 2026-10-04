from datetime import UTC, datetime

from agent_tools import dash_feed
from agent_tools.dash_chair_beat import beat_age_s, parse_status_line, read_status_record

NOW = datetime(2026, 10, 4, 12, 1, 0, tzinfo=UTC)
LINE = "chair 10-04 08:01 EDT | lanes 1/3 | lands 0 | limits 5h n/a weekly 10%/90% (est) | holding | needs chair: none"


def test_beat_47_seconds_before_now_is_47():
    assert beat_age_s("2026-10-04T12:00:13+00:00", NOW) == 47


def test_missing_heartbeat_is_none():
    assert beat_age_s(None, NOW) is None


def test_unparseable_heartbeat_is_none():
    assert beat_age_s("not a time", NOW) is None


def test_naive_heartbeat_is_none():
    assert beat_age_s("2026-10-04T12:00:13", NOW) is None


def test_status_line_reads_back_with_its_stamp():
    assert parse_status_line(LINE) == ("10-04 08:01 EDT", LINE)


def test_empty_or_missing_text_is_none_for_both():
    assert parse_status_line("") == (None, None)
    assert parse_status_line(None) == (None, None)


def test_non_status_last_line_is_none_for_both():
    assert parse_status_line(f"{LINE}\nTraceback (most recent call last):") == (None, None)


def test_last_status_line_wins():
    older = LINE.replace("08:01", "07:56")
    assert parse_status_line(f"{older}\n{LINE}\n\n") == ("10-04 08:01 EDT", LINE)


def test_tick_error_line_is_a_status_line():
    line = "chair 10-04 08:01 EDT | tick error: ValueError: boom"
    assert parse_status_line(line) == ("10-04 08:01 EDT", line)


def test_read_status_record_reads_file_and_tolerates_a_missing_one(tmp_path):
    log = tmp_path / "chair-loop.log"
    log.write_text(f"{LINE}\n", encoding="utf-8")
    assert read_status_record(log) == ("10-04 08:01 EDT", LINE)
    assert read_status_record(tmp_path / "absent.log") == (None, None)


def test_the_feed_reads_the_newest_store_row_over_an_older_log_line(tmp_path, monkeypatch):
    (tmp_path / "chair-loop.log").write_text(LINE.replace("08:01", "07:56") + "\n", encoding="utf-8")
    monkeypatch.setattr(dash_feed.run_store, "latest_chair_status", lambda _runs: {"line": LINE})
    assert dash_feed._chair_edge(tmp_path)["status"] == ("10-04 08:01 EDT", LINE)


def test_the_feed_reads_the_log_when_the_store_has_no_status_row(tmp_path, monkeypatch):
    (tmp_path / "chair-loop.log").write_text(LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(dash_feed.run_store, "latest_chair_status", lambda _runs: None)
    assert dash_feed._chair_edge(tmp_path)["status"] == ("10-04 08:01 EDT", LINE)
