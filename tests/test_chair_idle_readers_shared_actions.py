from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import chair_read_idle_backlog as backlog_mod
from agent_tools import chair_read_idle_open as open_mod
from agent_tools import pacing, run_store
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import FactsDeps, gather_facts, idle_stall_inputs, windowed_actions
from agent_tools.chair_read_idle_backlog import read_idle_backlog
from agent_tools.chair_read_idle_open import open_idle_stall, read_idle_open
from agent_tools.chair_read_stale import read_chair_actions

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
COLUMNS = ["ts", "kind", "target", "cause", "signature", "reason"]
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)


def _row(days_ago: int, kind: str, **rest: str) -> dict:
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "kind": kind, "target": "", "cause": "", "signature": "", "reason": "", **rest}


IN_WINDOW = [
    _row(2, "launch_epic"),
    _row(1, "land"),
    _row(1, "needs_chair", cause="idle_stall", signature="blocked:a", reason="a waits on b"),
]
STORE_ROWS = [*IN_WINDOW, _row(30, "land")]  # the 30 day old land is outside the 7 day window
LAST_LAND = "2026-10-06T12:00:00Z"


class FakeConn:
    """Applies the windowed query's bind values the way the store does: `ts >= cutoff OR kind IN kinds`."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []
        self.description = [(c,) for c in COLUMNS]
        self._found: list[dict] = []

    def execute(self, sql: str, params: tuple = ()):
        self.queries.append(sql)
        windowed = bool(params)
        self._found = [r for r in self.rows if not windowed or r["ts"] >= params[0] or r["kind"] in params[1:]]
        return self

    def fetchall(self) -> list[dict]:
        return self._found

    def close(self) -> None:
        pass


def _store(monkeypatch, tmp_path: Path) -> FakeConn:
    conn = FakeConn(STORE_ROWS)
    monkeypatch.setattr(run_store, "_open", lambda runs_dir: (conn, "?"))
    monkeypatch.setattr(backlog_mod, "read_docket", lambda *_a, **_k: None)
    monkeypatch.setattr(backlog_mod, "read_live_initiatives", lambda *_a: [])
    monkeypatch.setattr(open_mod, "open_needs_chair_ids", lambda _entries: {"blocked:a"})
    return conn


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def _deps(tmp_path: Path, seen: list) -> FactsDeps:
    actions = windowed_actions(lambda n: read_chair_actions(tmp_path / "runs", n), NOW)

    def idle_stall(n: datetime):
        got = idle_stall_inputs(
            read_idle_backlog(tmp_path, "files", 4, n, actions=actions()),
            [],
            [],
            read_idle_open(tmp_path, actions()),
            15,
        )
        seen.append(got)
        return got

    return FactsDeps(
        lease=lambda: {"holder": lease_holder("s", 7, "h"), "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(10.0, 168),
        policy=lambda: POLICY,
        docket=lambda: {"initiatives": [], "busy_lanes": 0, "max_in_flight": 2},
        approved=lambda: [],
        quarantined=lambda: [],
        stranded=lambda: [],
        attempts=lambda: [],
        has_patch=lambda initiative, task: False,
        live_initiatives=lambda: [],
        intake=lambda: [],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
        idle_stall=idle_stall,
        actions=actions,
    )


def test_both_idle_readers_share_the_one_chair_actions_query(tmp_path, monkeypatch):
    conn = _store(monkeypatch, tmp_path)
    seen: list = []
    gather_facts(_deps(tmp_path, seen), NOW)
    assert len([q for q in conn.queries if "chair_actions" in q]) == 1
    assert (seen[0]["last_progress_at"], seen[0]["open_signature"]) == (LAST_LAND, "blocked:a")


def test_the_backlog_reader_on_literal_rows_matches_its_result_on_the_shared_rows(tmp_path, monkeypatch):
    _store(monkeypatch, tmp_path)
    shared = windowed_actions(lambda n: read_chair_actions(tmp_path / "runs", n), NOW)()
    from_shared = read_idle_backlog(tmp_path, "files", 4, NOW, actions=shared)
    from_literal = read_idle_backlog(tmp_path, "files", 4, NOW, actions=IN_WINDOW)
    assert from_shared == from_literal
    assert from_literal["last_progress_at"] == LAST_LAND


def test_the_open_reader_on_literal_rows_matches_its_result_on_the_shared_rows(tmp_path, monkeypatch):
    _store(monkeypatch, tmp_path)
    shared = windowed_actions(lambda n: read_chair_actions(tmp_path / "runs", n), NOW)()
    from_shared = read_idle_open(tmp_path, shared)
    assert from_shared == read_idle_open(tmp_path, IN_WINDOW)
    assert from_shared == open_idle_stall(IN_WINDOW, {"blocked:a"}) == ("blocked:a", "a waits on b")


def test_given_rows_the_readers_make_no_query(tmp_path, monkeypatch):
    conn = _store(monkeypatch, tmp_path)
    read_idle_backlog(tmp_path, "files", 4, NOW, actions=IN_WINDOW)
    read_idle_open(tmp_path, IN_WINDOW)
    assert [q for q in conn.queries if "chair_actions" in q] == []
