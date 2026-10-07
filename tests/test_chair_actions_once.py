from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import pacing, run_store
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import FactsDeps, gather_facts, windowed_actions
from agent_tools.chair_read_stale import read_chair_actions, read_stale_candidates

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
COLUMNS = ["ts", "kind", "target", "initiative", "other"]
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)


def _row(days_ago: int, kind: str = "land", **rest: str) -> dict:
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return {"ts": ts, "kind": kind, "target": "", "initiative": "", "other": "", **rest}


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


def _store(monkeypatch, rows: list[dict]) -> FakeConn:
    conn = FakeConn(rows)
    monkeypatch.setattr(run_store, "_open", lambda runs_dir: (conn, "?"))
    return conn


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def test_one_gather_makes_one_chair_actions_query_and_every_reader_shares_its_rows(tmp_path, monkeypatch):
    conn = _store(monkeypatch, [_row(1, "steer_clear", initiative="x", other="y"), _row(2, "land", target="t1")])
    _write(tmp_path / "work/x/initiative.md", "---\nid: x\nrepo: /r\n---\n\nbody\n")
    _write(tmp_path / "work/x/p1/t1.md", "---\nid: t1\nstate: ready\n---\n\nbody\n")
    actions = windowed_actions(lambda n: read_chair_actions(tmp_path / "runs", n), NOW)
    deps = FactsDeps(
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
        idle_stall=lambda n: {
            "free_lanes": 0, "ready": 0, "queued": 0, "last_progress_at": None, "stall_minutes": 15, "hosts": [],
            "empty_stubs": [], "lands_waiting": [], "blocked_ready": [], "open_signature": None, "open_diagnosis": None,
        },
        actions=actions,
        stale_candidates=lambda n: read_stale_candidates(tmp_path, n, attempts=[], actions=actions()),
    )
    facts = gather_facts(deps, NOW)
    assert len([q for q in conn.queries if "chair_actions" in q]) == 1
    assert facts["steer_streaks"] == {"x|y": 1}
    assert facts["stale_candidates"][0]["last_chair_action"] == _row(2)["ts"]


def test_windowed_actions_loads_once_and_hands_back_the_same_rows():
    loads: list[datetime] = []
    source = [{"ts": "1"}]
    actions = windowed_actions(lambda n: loads.append(n) or source, NOW)
    first, second = actions(), actions()
    assert loads == [NOW]
    assert first is second and first == ({"ts": "1"},) and first[0] is not source[0]


def test_a_row_older_than_seven_days_is_not_read_and_one_inside_the_window_is(tmp_path, monkeypatch):
    _store(monkeypatch, [_row(8, target="old"), _row(6, target="new")])
    assert [r["target"] for r in read_chair_actions(tmp_path / "runs", NOW)] == ["new"]


def test_an_old_needs_chair_row_is_still_read_because_decompose_dedupe_needs_it(tmp_path, monkeypatch):
    _store(monkeypatch, [_row(30, "needs_chair", target="old"), _row(30, "land", target="gone")])
    assert [r["target"] for r in read_chair_actions(tmp_path / "runs", NOW)] == ["old"]


def test_without_now_every_row_is_read(tmp_path, monkeypatch):
    conn = _store(monkeypatch, [_row(30), _row(1)])
    assert len(read_chair_actions(tmp_path / "runs")) == 2
    assert conn.queries == ["SELECT * FROM chair_actions"]
