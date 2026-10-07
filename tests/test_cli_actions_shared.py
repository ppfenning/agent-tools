"""One chair tick reads `chair_actions` once: the gather, `held()` and the watch readers share `_chair_run_deps`'s rows."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import chair_facts, chair_run, cli, run_store

NOW = datetime.now(UTC)  # the tick's own clock is the real one, so the rows are dated from it
COLUMNS = ["ts", "kind", "target", "initiative", "phase", "repo", "pr", "commit", "outcome"]


def _row(days_ago: int, kind: str, **rest: object) -> dict:
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return dict.fromkeys(COLUMNS, "") | {"ts": ts, "kind": kind} | rest


def _watch(days_ago: int, commit: str = "abc") -> dict:
    return _row(days_ago, "revert_watch", initiative="i1", phase="p1", repo="/r/i1", pr="7", commit=commit)


class CountingConn:
    """Answers the `SELECT * FROM chair_actions` row read the way the store does and refuses every other query.

    The meter, housekeeping and tuning readers run their own narrow `chair_actions` queries; they are refused and not counted."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.queries: list[str] = []
        self.description = [(c,) for c in COLUMNS]
        self._found: list[dict] = []

    def execute(self, sql: str, params: tuple = ()):
        if not sql.startswith("SELECT * FROM chair_actions"):
            raise sqlite3.OperationalError("no such table")
        self.queries.append(sql)
        windowed = bool(params)
        self._found = [r for r in self.rows if not windowed or r["ts"] >= params[0] or r["kind"] in params[1:]]
        return self

    def fetchall(self) -> list[dict]:
        return self._found

    def close(self) -> None:
        pass


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "runs").mkdir()
    (tmp_path / "work" / "i1").mkdir(parents=True)
    (tmp_path / "work" / "i1" / "initiative.md").write_text("---\nrepo: /r/i1\n---\n", encoding="utf-8")
    return tmp_path


def _tick(monkeypatch, ws: Path, rows: list[dict]) -> tuple[CountingConn, chair_run.RunDeps]:
    conn = CountingConn(rows)
    monkeypatch.setattr(run_store, "_open", lambda runs_dir: (conn, "?"))
    deps = cli._chair_run_deps(ws / "runs", {}, "chair", 1, "h", True, print, ws / "p.yaml", "files")
    return conn, deps


def test_one_gather_held_and_the_watch_readers_issue_one_chair_actions_query(monkeypatch, ws) -> None:
    conn, deps = _tick(monkeypatch, ws, [_watch(1)])
    chair_facts.gather_facts(deps.facts_deps, NOW)
    deps.perform([], deps.exec_deps, deps.current_epoch, True)
    deps.facts_deps.land_watches()
    deps.facts_deps.land_outcomes()
    assert len(conn.queries) == 1


def test_a_beat_ends_the_shared_actions_read(monkeypatch, ws) -> None:
    conn, deps = _tick(monkeypatch, ws, [])
    deps.facts_deps.actions()
    deps.facts_deps.actions()
    first = len(conn.queries)
    deps.beat()
    deps.facts_deps.actions()
    assert (first, len(conn.queries)) == (1, 2)


def test_held_reads_the_rows_the_gather_loaded(monkeypatch, ws) -> None:
    conn, deps = _tick(monkeypatch, ws, [_watch(1)])
    chair_facts.gather_facts(deps.facts_deps, NOW)
    deps.perform([], deps.exec_deps, deps.current_epoch, False)
    assert len(conn.queries) == 1


def test_a_watch_ten_days_old_is_still_pending(monkeypatch, ws) -> None:
    _, deps = _tick(monkeypatch, ws, [_watch(10), _row(10, "land", target="gone")])
    assert [w["commit"] for w in deps.facts_deps.land_watches()] == ["abc"]


def test_a_watch_resolved_after_ten_days_is_no_longer_pending(monkeypatch, ws) -> None:
    resolved = _row(9, "revert_watch_resolved", commit="abc", outcome="held")
    _, deps = _tick(monkeypatch, ws, [_watch(10), resolved])
    assert (deps.facts_deps.land_watches(), deps.facts_deps.land_outcomes()) == ([], {"i1": ["held"]})


def test_an_old_row_of_another_kind_is_still_dropped_by_the_window(monkeypatch, ws) -> None:
    _, deps = _tick(monkeypatch, ws, [_row(10, "land", target="gone"), _row(2, "land", target="kept")])
    assert [r["target"] for r in deps.facts_deps.actions()] == ["kept"]
