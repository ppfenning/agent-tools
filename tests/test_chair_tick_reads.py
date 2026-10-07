from __future__ import annotations

import collections
import datetime
from pathlib import Path

import pytest

from agent_tools import chair_facts, cli, draft_list, run_store
from agent_tools import chair_read_approved as approved
from agent_tools import chair_read_attempts as attempts
from agent_tools import chair_read_docket as docket
from agent_tools import chair_read_idle_backlog as idle
from agent_tools import chair_read_intake as intake
from agent_tools import chair_read_patch as patch
from agent_tools import chair_read_quarantined as quarantined
from agent_tools import chair_read_stale as stale

NOW = datetime.datetime(2026, 10, 7, 12, 0, tzinfo=datetime.UTC)
ATTEMPT = "attempts:\n  - {run: i1-1, ts: '2026-10-06T10:00:00Z', kind: build}\n"
STORE_ROWS = [{"initiative": "i1", "task_id": "t1", "state": "approved"}]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "runs").mkdir()
    (tmp_path / "work" / "i1").mkdir(parents=True)
    (tmp_path / "work" / "i1" / "initiative.md").write_text("---\nrepo: /r/i1\n---\n", encoding="utf-8")
    for phase, task, state in [("p1", "t1", "ready"), ("p1", "t2", "ready"), ("p2", "t3", "blocked")]:
        path = tmp_path / "work" / "i1" / phase / f"{task}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"---\nid: {task}\nstate: {state}\n{ATTEMPT}---\nbody {task}\n", encoding="utf-8")
    return tmp_path


def test_newest_runs_keeps_the_last_run_per_task_and_skips_rows_without_one() -> None:
    rows = [
        {"initiative": "i1", "task": "t1", "run": "i1-1"},
        {"initiative": "i1", "task": "t1", "run": "i1-2"},
        {"initiative": "i1", "task": "t1", "run": None},
        {"initiative": "i1", "task": "t2", "run": ""},
        {"initiative": "i2", "task": "t1", "run": "i2-1"},
    ]
    assert patch.newest_runs(rows) == {("i1", "t1"): "i1-2", ("i2", "t1"): "i2-1"}


def _deps(ws: Path, mode: str):
    return cli._chair_run_deps(ws / "runs", {}, "chair", 1, "h", True, print, ws / "p.yaml", mode)


def test_one_gather_with_three_quarantines_reads_attempts_and_stranded_inputs_once(monkeypatch, ws) -> None:
    calls: collections.Counter = collections.Counter()
    real_attempts, real_stranded = attempts.read_attempts, cli._chair_stranded_inputs
    monkeypatch.setattr(attempts, "read_attempts", lambda *a, **k: calls.update(["attempts"]) or real_attempts(*a, **k))
    monkeypatch.setattr(cli, "_chair_stranded_inputs", lambda *a, **k: calls.update(["stranded"]) or real_stranded(*a, **k))
    deps = _deps(ws, "files").facts_deps
    chair_facts.gather_facts(deps, NOW)
    assert (dict(calls), len(deps.quarantined())) == ({"attempts": 1, "stranded": 1}, 3)


def test_one_gather_reads_each_work_path_and_the_store_rows_once(monkeypatch, ws) -> None:
    reads: collections.Counter = collections.Counter()
    store_reads: list[int] = []
    real_read_text = Path.read_text

    def counting(self: Path, *a, **k) -> str:
        reads[self] += 1
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    monkeypatch.setattr(run_store, "work_items", lambda *_a: store_reads.append(1) or [])
    chair_facts.gather_facts(_deps(ws, "store").facts_deps, NOW)
    work = {p: n for p, n in reads.items() if (ws / "work") in p.parents}
    assert (len(work), set(work.values()), len(store_reads)) == (4, {1}, 1)


def test_a_beat_ends_the_tick_reads(monkeypatch, ws) -> None:
    store_reads: list[int] = []
    monkeypatch.setattr(run_store, "work_items", lambda *_a: store_reads.append(1) or [])
    deps = _deps(ws, "store")
    deps.facts_deps.quarantined()
    deps.facts_deps.attempts()
    first = len(store_reads)
    deps.beat()
    deps.facts_deps.quarantined()
    assert (first, len(store_reads)) == (1, 2)


def test_read_work_files_holds_task_and_initiative_files_by_path(ws) -> None:
    files = quarantined.read_work_files(ws)
    assert (sorted(p.name for p in quarantined.task_files(ws, files)), list(quarantined.initiative_files(ws, files).values())) == (
        ["t1.md", "t2.md", "t3.md"], ["---\nrepo: /r/i1\n---\n"],
    )


@pytest.mark.parametrize("mode", ["files", "store"])
def test_every_reader_returns_the_same_with_files_as_without(monkeypatch, ws, mode: str) -> None:
    monkeypatch.setattr(run_store, "work_items", lambda *_a: STORE_ROWS)
    files = quarantined.read_work_files(ws)
    n = 3
    same = [
        (docket.read_docket(ws, mode, n, lambda: NOW), docket.read_docket(ws, mode, n, lambda: NOW, files=files)),
        (approved.read_approved(ws, mode), approved.read_approved(ws, mode, files=files)),
        (attempts.read_attempts(ws), attempts.read_attempts(ws, files)),
        (idle.read_idle_backlog(ws, mode, n, NOW), idle.read_idle_backlog(ws, mode, n, NOW, files=files)),
        (quarantined.read_quarantined(ws, mode), quarantined.read_quarantined(ws, mode, files=files)),
        (cli._chair_stranded_inputs(ws, mode), cli._chair_stranded_inputs(ws, mode, files=files)),
        (stale.read_stale_candidates(ws, NOW), stale.read_stale_candidates(ws, NOW, files, attempts.read_attempts(ws))),
        (intake.read_intake(ws), intake.read_intake(ws, quarantined.initiative_files(ws, files))),
        (draft_list.read_drafts(ws / "work", "2026-10-07T12:00:00Z"), draft_list.read_drafts(ws / "work", "2026-10-07T12:00:00Z", quarantined.initiative_files(ws, files))),
    ]
    assert all(plain == given for plain, given in same)


@pytest.mark.parametrize("rows", [[], STORE_ROWS])
def test_every_reader_returns_the_same_with_store_rows_as_without(monkeypatch, ws, rows: list) -> None:
    monkeypatch.setattr(run_store, "work_items", lambda *_a: rows)
    n = 3
    same = [
        (docket.read_docket(ws, "store", n, lambda: NOW), docket.read_docket(ws, "store", n, lambda: NOW, store_rows=rows)),
        (approved.read_approved(ws, "store"), approved.read_approved(ws, "store", store_rows=rows)),
        (idle.read_idle_backlog(ws, "store", n, NOW), idle.read_idle_backlog(ws, "store", n, NOW, store_rows=rows)),
        (quarantined.read_quarantined(ws, "store"), quarantined.read_quarantined(ws, "store", store_rows=rows)),
        (cli._chair_stranded_inputs(ws, "store"), cli._chair_stranded_inputs(ws, "store", store_rows=rows)),
    ]
    assert all(plain == given for plain, given in same)
    assert approved.read_approved(ws, "store", store_rows=STORE_ROWS) != approved.read_approved(ws, "store", store_rows=[])
