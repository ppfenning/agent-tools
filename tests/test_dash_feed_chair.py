import json
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import dash_feed

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dash_feed_v1.json").read_text(encoding="utf-8"))

AT = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)  # 09-28 20:00 EDT; local midnight is 09-28 04:00Z
ROW = {"holder": "chair", "state": "live"}
LEASE = {"holder": "chair@omarchy:12345", "epoch": 7}
RECORD = {"claude_session": "a1b2c3d4-0000", "heartbeat_at": "2026-09-28T23:59:56+00:00"}
STATUS = ("09-28 19:59 EDT", "chair 09-28 19:59 EDT | landed 2, launched 1")
ACTION = {"kind": "land", "target": "dash-feed/p2-feed", "since": "2026-09-28T23:59:50Z"}
ROWS = [
    {"ts": "2026-09-28T20:00:00Z", "kind": "land", "status": "landed"},
    {"ts": "2026-09-28T21:00:00Z", "kind": "land_phase", "status": "landed"},
    {"ts": "2026-09-28T22:00:00Z", "kind": "launch_epic", "status": "done"},
    {"ts": "2026-09-28T22:30:00Z", "kind": "retry", "status": "failed"},
    {"ts": "2026-09-28T03:00:00Z", "kind": "land", "status": "landed"},  # before local midnight
]


def test_chair_section_from_staged_inputs_equals_the_fixture_chair_block():
    chair = dash_feed._chair_v1(
        ROW, LEASE, RECORD, AT, STATUS, ACTION, ROWS, 1,
        last_housekeeping_at="2026-09-28T23:00:00Z", inbox_entries=[{"to": "pat"}], drafts=[1, 2],
    )

    assert chair == FIXTURE["chair"]


def test_chair_section_with_no_action_running_has_a_null_current_action():
    chair = dash_feed._chair_v1(ROW, LEASE, RECORD, AT, STATUS, None, [], 0)

    assert chair["current_action"] is None
    assert chair["today"] == {"lands": 0, "launches": 0, "refused_or_failed": 0, "needs_chair_open": 0}
    extras = {k: FIXTURE["chair"][k] for k in ("lands_today", "phases_today", "needs_you", "drafts", "housekeeping_age_s")}
    assert {**chair, "current_action": ACTION, "today": FIXTURE["chair"]["today"], **extras} == FIXTURE["chair"]


def test_chair_section_with_no_heartbeat_or_status_reads_zero_and_empty_strings():
    chair = dash_feed._chair_v1(ROW, LEASE, {}, AT)

    assert (chair["beat_age_s"], chair["last_tick_at"], chair["last_status"], chair["tick_age_s"]) == (0, "", "", None)


def test_tick_age_reads_the_eastern_stamp_and_is_none_when_absent_or_unreadable():
    assert dash_feed._tick_age_s("09-28 19:59 EDT", AT) == 60
    assert dash_feed._tick_age_s("12-31 23:59 EST", datetime(2027, 1, 1, 5, 0, tzinfo=UTC)) == 60
    assert [dash_feed._tick_age_s(text, AT) for text in ("", None, "not a stamp")] == [None, None, None]


# The store's rows: the lease row with the tick in its `status` JSON, and `chair_actions` rows with ISO `ts`.
STORE_LEASE = {
    "name": "chair", "holder": "chair@omarchy:12345", "epoch": 7,
    "heartbeat_at": "2026-09-28T23:59:56+00:00", "expires_at": "2026-09-29T00:05:00+00:00",
    "status": json.dumps({"tick_at": "2026-09-28T23:59:00+00:00", "status": STATUS[1], "current_action": ACTION}),
}
HOUSEKEEPING = {"ts": "2026-09-28T23:00:00Z", "kind": "housekeeping", "status": "done"}
ENTRIES = [{"to": "pat", "ref": "coxswain://needs_chair/some-task"}]


def _stage_local(runs, *, epoch=7, holder="chair@omarchy:12345"):
    (runs / "chair.lease.json").write_text(json.dumps({"holder": holder, "epoch": epoch}), encoding="utf-8")
    (runs / "chair.json").write_text(json.dumps(RECORD), encoding="utf-8")
    (runs / "chair-loop.log").write_text(STATUS[1] + "\n", encoding="utf-8")
    log = [*ROWS, HOUSEKEEPING, {"ts": ACTION["since"], "kind": "land", "initiative": "dash-feed", "phase": "p2-feed"}]
    (runs / "chair.actions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in log), encoding="utf-8")


def _fetch(monkeypatch, result):
    monkeypatch.setattr(dash_feed, "read_chair_rows", lambda runs_dir, now, offset: result)


def test_chair_section_from_staged_store_rows_equals_the_fixture_chair_block(tmp_path, monkeypatch):
    _fetch(monkeypatch, (STORE_LEASE, [*ROWS, HOUSEKEEPING]))
    (tmp_path / "chair.json").write_text(json.dumps(RECORD), encoding="utf-8")

    assert dash_feed._chair_section(tmp_path, {}, ENTRIES, [1, 2], AT) == FIXTURE["chair"]


def test_chair_section_with_no_store_rows_equals_the_same_block_from_local_record_files(tmp_path, monkeypatch):
    _fetch(monkeypatch, None)
    _stage_local(tmp_path)

    assert dash_feed._chair_section(tmp_path, ROW, ENTRIES, [1, 2], AT) == FIXTURE["chair"]


def test_the_store_row_wins_when_the_local_record_files_disagree(tmp_path, monkeypatch):
    _fetch(monkeypatch, (STORE_LEASE, [*ROWS, HOUSEKEEPING]))
    _stage_local(tmp_path, epoch=3, holder="old@elsewhere:1")

    chair = dash_feed._chair_section(tmp_path, {"holder": "old@elsewhere:1", "state": "stale"}, ENTRIES, [1, 2], AT)

    assert (chair["epoch"], chair["holder"], chair["host"], chair["liveness"]) == (7, "chair", "omarchy", "live")
