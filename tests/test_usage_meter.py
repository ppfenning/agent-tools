import json
from datetime import UTC, datetime, timedelta

from agent_tools import pacing, run_store, usage_meter


def _fake_cost_since(monkeypatch, dollars, calls):
    def fake(runs_dir, since, until=None):
        calls.append(since)
        return dollars

    monkeypatch.setattr(run_store, "cost_since", fake)

_VALID_DOC = {
    "five_hour": {"used_percentage": 16, "resets_at": 1790616000},
    "seven_day": {"used_percentage": 35, "resets_at": 1791100800},
    "observed_at": "2026-09-28T14:41:31Z",
    "source": "claude-code statusline",
}


def test_parse_valid_doc_yields_both_entries_and_observed_at():
    meter = usage_meter.parse(_VALID_DOC)
    assert meter.five_hour.used_percentage == 16.0
    assert meter.five_hour.resets_at == datetime.fromtimestamp(1790616000, tz=UTC)
    assert meter.seven_day.used_percentage == 35.0
    assert meter.seven_day.resets_at == datetime.fromtimestamp(1791100800, tz=UTC)
    assert meter.observed_at == datetime.fromisoformat("2026-09-28T14:41:31Z")


def test_parse_returns_none_when_resets_at_is_missing():
    doc = {
        "five_hour": {"used_percentage": 16},
        "seven_day": {"used_percentage": 35, "resets_at": 1791100800},
        "observed_at": "2026-09-28T14:41:31Z",
    }
    assert usage_meter.parse(doc) is None


def test_parse_returns_none_when_resets_at_is_out_of_range():
    doc = {
        "five_hour": {"used_percentage": 16, "resets_at": 10**20},
        "seven_day": {"used_percentage": 35, "resets_at": 1791100800},
        "observed_at": "2026-09-28T14:41:31Z",
    }
    assert usage_meter.parse(doc) is None


def test_fresh_is_true_at_exactly_max_age():
    observed_at = datetime(2026, 9, 28, 14, 41, 31, tzinfo=UTC)
    meter = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(16.0, observed_at),
        seven_day=usage_meter.MeterEntry(35.0, observed_at),
        observed_at=observed_at,
    )
    now = observed_at + timedelta(minutes=15)
    assert usage_meter.fresh(meter, now, timedelta(minutes=15)) is True


def test_fresh_is_false_one_second_past_max_age():
    observed_at = datetime(2026, 9, 28, 14, 41, 31, tzinfo=UTC)
    meter = usage_meter.Meter(
        five_hour=usage_meter.MeterEntry(16.0, observed_at),
        seven_day=usage_meter.MeterEntry(35.0, observed_at),
        observed_at=observed_at,
    )
    now = observed_at + timedelta(minutes=15, seconds=1)
    assert usage_meter.fresh(meter, now, timedelta(minutes=15)) is False


def test_as_window_produces_the_hand_worked_numbers():
    resets_at = datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC)
    entry = usage_meter.MeterEntry(used_percentage=16.0, resets_at=resets_at)
    span = timedelta(hours=5)
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)  # 2h after start = resets_at - span

    window = usage_meter.as_window(entry, now, span)

    assert window.start == datetime(2026, 9, 28, 14, 41, 31, tzinfo=UTC)
    assert window.end == resets_at
    assert window.spent_usd == 16.0
    assert window.ceiling_usd == 100.0
    assert window.burn_usd_per_hour == 8.0
    assert window.runs_in_flight == 0


def test_as_window_with_no_elapsed_time_reports_infinite_burn_instead_of_raising():
    resets_at = datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC)
    entry = usage_meter.MeterEntry(used_percentage=16.0, resets_at=resets_at)
    now = datetime(2026, 9, 28, 13, 41, 31, tzinfo=UTC)  # 1h before start: clock skew

    assert usage_meter.as_window(entry, now, timedelta(hours=5)).burn_usd_per_hour == float("inf")


def _window(spent: float) -> pacing.Window:
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    return pacing.Window(
        start=now - timedelta(hours=1),
        end=now,
        spent_usd=spent,
        ceiling_usd=100.0,
        burn_usd_per_hour=spent,
        runs_in_flight=0,
    )


def test_prefer_returns_the_meter_window_when_given_one():
    meter_window = _window(1.0)
    estimate_window = _window(2.0)
    assert usage_meter.prefer(meter_window, estimate_window) is meter_window


def test_prefer_returns_the_estimate_window_when_meter_window_is_none():
    estimate_window = _window(2.0)
    assert usage_meter.prefer(None, estimate_window) is estimate_window


def test_read_returns_none_for_a_missing_file(tmp_path):
    assert usage_meter.read(tmp_path / "rate-limits.json") is None


def test_read_returns_none_for_unparseable_json(tmp_path):
    path = tmp_path / "rate-limits.json"
    path.write_text("not json")
    assert usage_meter.read(path) is None


def test_read_returns_none_for_invalid_utf8_bytes(tmp_path):
    path = tmp_path / "rate-limits.json"
    path.write_bytes(b"\xff\xfe{")
    assert usage_meter.read(path) is None


def test_read_returns_none_for_a_non_object_json_document(tmp_path):
    path = tmp_path / "rate-limits.json"
    path.write_text("[1, 2]")
    assert usage_meter.read(path) is None


def test_read_parses_a_valid_file_the_same_as_parse(tmp_path):
    path = tmp_path / "rate-limits.json"
    path.write_text(json.dumps(_VALID_DOC))
    assert usage_meter.read(path) == usage_meter.parse(_VALID_DOC)


def test_record_implied_ceiling_writes_at_five_percent_floor(tmp_path, monkeypatch):
    path = tmp_path / "implied.json"
    entry = usage_meter.MeterEntry(used_percentage=5.0, resets_at=datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC))
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)  # 2h into the 5h window: past the one-hour guard
    _fake_cost_since(monkeypatch, 3.0, [])

    usage_meter.record_implied_ceiling("five_hour", entry, tmp_path, now, path=path)

    doc = json.loads(path.read_text())
    assert doc["five_hour"]["ceiling_usd"] == 60.0
    assert doc["five_hour"]["observed_at"] == now.isoformat()


def test_record_implied_ceiling_one_point_below_floor_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "implied.json"
    entry = usage_meter.MeterEntry(used_percentage=4.0, resets_at=datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC))
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)

    def fail(*args, **kwargs):
        raise AssertionError("cost_since must not be called below the floor")

    monkeypatch.setattr(run_store, "cost_since", fail)

    usage_meter.record_implied_ceiling("five_hour", entry, tmp_path, now, path=path)

    assert path.exists() is False


def test_record_implied_ceiling_merges_with_an_existing_kind(tmp_path, monkeypatch):
    path = tmp_path / "implied.json"
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    path.write_text(json.dumps({"weekly": {"ceiling_usd": 200.0, "observed_at": now.isoformat()}}))
    entry = usage_meter.MeterEntry(used_percentage=10.0, resets_at=datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC))
    _fake_cost_since(monkeypatch, 5.0, [])

    usage_meter.record_implied_ceiling("five_hour", entry, tmp_path, now, path=path)

    doc = json.loads(path.read_text())
    assert doc["weekly"]["ceiling_usd"] == 200.0
    assert doc["five_hour"]["ceiling_usd"] == 50.0


def test_record_implied_ceiling_calibrates_over_the_meters_own_window(tmp_path, monkeypatch):
    path = tmp_path / "implied.json"
    resets_at = datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC)
    entry = usage_meter.MeterEntry(used_percentage=50.0, resets_at=resets_at)
    now = resets_at - timedelta(hours=3)  # 2h into the 5h window: past the one-hour guard
    calls = []
    _fake_cost_since(monkeypatch, 40.0, calls)

    usage_meter.record_implied_ceiling("five_hour", entry, tmp_path, now, path=path)

    assert calls == [(resets_at - timedelta(hours=5)).isoformat()]
    doc = json.loads(path.read_text())
    assert doc["five_hour"]["ceiling_usd"] == 80.0


def test_record_implied_ceiling_before_one_hour_elapsed_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "implied.json"
    resets_at = datetime(2026, 9, 28, 19, 41, 31, tzinfo=UTC)
    entry = usage_meter.MeterEntry(used_percentage=50.0, resets_at=resets_at)
    window_start = resets_at - timedelta(hours=5)
    now = window_start + timedelta(minutes=30)

    def fail(*args, **kwargs):
        raise AssertionError("cost_since must not be called before an hour of the window has elapsed")

    monkeypatch.setattr(run_store, "cost_since", fail)

    usage_meter.record_implied_ceiling("five_hour", entry, tmp_path, now, path=path)

    assert path.exists() is False


def test_usable_percentage_reads_back_a_calibrated_stale_reading(tmp_path, monkeypatch):
    path = tmp_path / "implied.json"
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    observed_at = now - timedelta(minutes=20)
    resets_at = now + timedelta(hours=2)
    entry = usage_meter.MeterEntry(used_percentage=40.0, resets_at=resets_at)
    path.write_text(json.dumps({"five_hour": {"ceiling_usd": 100.0, "observed_at": now.isoformat()}}))
    calls = []
    _fake_cost_since(monkeypatch, 10.0, calls)

    result = usage_meter.usable_percentage("five_hour", entry, observed_at, now, tmp_path, ceiling_usd=None, path=path)

    assert calls == [observed_at.isoformat()]
    assert result == 50.0


def test_usable_percentage_returns_none_past_resets_at(tmp_path, monkeypatch):
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    observed_at = now - timedelta(minutes=20)
    resets_at = now - timedelta(minutes=1)
    entry = usage_meter.MeterEntry(used_percentage=90.0, resets_at=resets_at)

    def fail(*args, **kwargs):
        raise AssertionError("cost_since must not be called for an expired reading")

    monkeypatch.setattr(run_store, "cost_since", fail)

    result = usage_meter.usable_percentage(
        "five_hour", entry, observed_at, now, tmp_path, ceiling_usd=100.0, path=tmp_path / "implied.json"
    )

    assert result is None


def test_implied_ceiling_returns_the_value_at_exactly_seven_days(tmp_path):
    path = tmp_path / "implied.json"
    observed_at = datetime(2026, 9, 21, 16, 41, 31, tzinfo=UTC)
    path.write_text(json.dumps({"five_hour": {"ceiling_usd": 60.0, "observed_at": observed_at.isoformat()}}))
    now = observed_at + timedelta(days=7)

    assert usage_meter.implied_ceiling("five_hour", now, path=path) == 60.0


def test_implied_ceiling_returns_none_one_second_past_seven_days(tmp_path):
    path = tmp_path / "implied.json"
    observed_at = datetime(2026, 9, 21, 16, 41, 31, tzinfo=UTC)
    path.write_text(json.dumps({"five_hour": {"ceiling_usd": 60.0, "observed_at": observed_at.isoformat()}}))
    now = observed_at + timedelta(days=7, seconds=1)

    assert usage_meter.implied_ceiling("five_hour", now, path=path) is None


def test_implied_ceiling_returns_none_for_a_missing_kind(tmp_path):
    path = tmp_path / "implied.json"
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    path.write_text(json.dumps({"weekly": {"ceiling_usd": 200.0, "observed_at": now.isoformat()}}))

    assert usage_meter.implied_ceiling("five_hour", now, path=path) is None


def test_implied_ceiling_returns_none_for_a_missing_file(tmp_path):
    now = datetime(2026, 9, 28, 16, 41, 31, tzinfo=UTC)
    assert usage_meter.implied_ceiling("five_hour", now, path=tmp_path / "implied.json") is None
