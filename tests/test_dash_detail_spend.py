import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import run_store, usage_meter
from agent_tools.dash_detail_spend import _rows_since, build, group_spend, project_to_hard_stop
from agent_tools.usage_meter import Meter, MeterEntry


def row(initiative, role, model, task_id, cost_usd):
    return {"initiative": initiative, "role": role, "model": model, "task_id": task_id, "cost_usd": cost_usd}


ROWS = [
    row("alpha", "build", "sonnet", "alpha-1", 1.0),
    row("alpha", "build", "sonnet", "alpha-1", 0.5),
    row("alpha", "review", "haiku", "alpha-2", 2.0),
    row("beta", "build", "sonnet", "beta-1", 4.0),
    row("beta", "review", "haiku", "beta-2", 0.25),
    row("beta", "review", "sonnet", "beta-2", 0.25),
]


def test_group_spend_totals_by_initiative_role_and_model():
    got = group_spend(ROWS, top_n=2)
    assert got["attributed_usd"] == 8.0
    assert got["by_initiative"] == {"alpha": 3.5, "beta": 4.5}
    assert got["by_role"] == {"build": 5.5, "review": 2.5}
    assert got["by_model"] == {"sonnet": 5.75, "haiku": 2.25}


def test_group_spend_costliest_tasks_are_summed_ordered_and_capped():
    got = group_spend(ROWS, top_n=2)
    assert got["top_tasks"] == [
        {"task_id": "beta-1", "cost_usd": 4.0},
        {"task_id": "alpha-2", "cost_usd": 2.0},
    ]


def test_group_spend_ties_break_on_task_id_ascending():
    tied = [row("alpha", "build", "sonnet", "z-task", 1.0), row("alpha", "build", "sonnet", "a-task", 1.0)]
    got = group_spend(tied, top_n=5)
    assert got["top_tasks"] == [
        {"task_id": "a-task", "cost_usd": 1.0},
        {"task_id": "z-task", "cost_usd": 1.0},
    ]


def test_group_spend_of_no_rows_is_all_empty():
    got = group_spend([], top_n=5)
    assert got == {"attributed_usd": 0.0, "by_initiative": {}, "by_role": {}, "by_model": {}, "top_tasks": []}


def test_group_spend_sums_cheap_rows_at_full_precision_before_rounding_once():
    # Regression: `_totals_by` used to round its running total on every addition
    # (`round(total + cost, 2)`), so a $0.004 row rounded away to nothing before the
    # next one could add to it, and a category built only of such rows read as $0.00.
    cheap = [row("alpha", "build", "sonnet", f"task-{i}", 0.004) for i in range(1000)]
    got = group_spend(cheap, top_n=1)
    assert got["by_model"]["sonnet"] == 4.0
    assert got["by_role"]["build"] == 4.0


def test_project_to_hard_stop_hits_before_reset():
    got = project_to_hard_stop(used_pct=60.0, ceiling_pct=100.0, burn_pct_per_hour=10.0, hours_until_reset=20.0)
    assert got == {"remaining_pct": 40.0, "hours_to_exhaustion": 4.0, "hits_before_reset": True}


def test_project_to_hard_stop_resets_before_it_hits():
    got = project_to_hard_stop(used_pct=10.0, ceiling_pct=100.0, burn_pct_per_hour=1.0, hours_until_reset=20.0)
    assert got == {"remaining_pct": 90.0, "hours_to_exhaustion": 90.0, "hits_before_reset": False}


def test_project_to_hard_stop_zero_burn_never_exhausts():
    got = project_to_hard_stop(used_pct=10.0, ceiling_pct=100.0, burn_pct_per_hour=0.0, hours_until_reset=20.0)
    assert got["hours_to_exhaustion"] == float("inf")
    assert got["hits_before_reset"] is False


def test_build_against_an_empty_runs_dir_has_the_expected_shape(tmp_path: Path):
    got = build(tmp_path, "2026-09-28T12:00:00+00:00")
    assert set(got) == {"today", "week", "projection"}
    for window in (got["today"], got["week"]):
        assert window["total_usd"] is None
        assert window["attributed_usd"] == 0.0
        assert window["unattributed_usd"] is None
        assert window["by_initiative"] == {}
        assert window["top_tasks"] == []


# `_rows_since` and `build`'s use of `run_store.usages`/`work_items` and `usage_meter.read`/`as_window` is
# checked here against those functions' confirmed real shapes: `usages(runs_dir)` returns one
# `{"run_id": ..., "calls": [...], "summary": {...}}` per run id, per `run_store._store_usage`, and a call
# carries `role`, `model`, `task_id`, `cost_usd`, `ts` among other fields, per `run_store.call_from_row` and
# `run_store._SAME`. `work_items(runs_dir)` rows carry `task_id` and `initiative`, per `run_store._items` in
# `dash_detail_initiative.py`, which reads the same rows the same way.


def _usage_file(tmp_path: Path, run_id: str, calls: list[dict]) -> None:
    (tmp_path / f"{run_id}.usage.json").write_text(json.dumps({"run_id": run_id, "calls": calls, "summary": {}}))


def _cox_db(tmp_path: Path, rows: list[tuple[float, str]]) -> None:
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE node_calls (cost_usd REAL, ts TEXT)")
    conn.executemany("INSERT INTO node_calls (cost_usd, ts) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_rows_since_joins_a_usage_file_calls_task_id_to_its_work_items_initiative(tmp_path: Path, monkeypatch):
    _usage_file(
        tmp_path,
        "r1",
        [
            {"role": "build", "model": "sonnet", "task_id": "task-a", "cost_usd": 1.5, "ts": "2026-09-28T01:00:00+00:00"},
            {"role": "review", "model": "haiku", "task_id": "task-b", "cost_usd": 2.0, "ts": "2026-09-27T01:00:00+00:00"},
        ],
    )
    monkeypatch.setattr(run_store, "work_items", lambda *a, **k: [{"task_id": "task-a", "initiative": "epic-x"}])

    got = _rows_since(tmp_path, "2026-09-28T00:00:00+00:00")

    assert got == [
        {"initiative": "epic-x", "role": "build", "model": "sonnet", "task_id": "task-a", "cost_usd": 1.5},
    ]


def test_build_against_a_populated_runs_dir_wires_cost_since_and_the_grouped_rows(tmp_path: Path, monkeypatch):
    _cox_db(tmp_path, [(1.5, "2026-09-28T01:00:00+00:00"), (2.0, "2026-09-27T01:00:00+00:00")])
    _usage_file(
        tmp_path,
        "r1",
        [
            {"role": "build", "model": "sonnet", "task_id": "task-a", "cost_usd": 1.5, "ts": "2026-09-28T01:00:00+00:00"},
            {"role": "review", "model": "haiku", "task_id": "task-b", "cost_usd": 2.0, "ts": "2026-09-27T01:00:00+00:00"},
        ],
    )
    monkeypatch.setattr(run_store, "work_items", lambda *a, **k: [{"task_id": "task-a", "initiative": "epic-x"}])
    monkeypatch.setattr(usage_meter, "read", lambda: None)

    got = build(tmp_path, "2026-09-28T12:00:00+00:00")

    assert got["today"]["total_usd"] == 1.5
    assert got["today"]["attributed_usd"] == 1.5
    assert got["today"]["unattributed_usd"] == 0.0
    assert got["today"]["by_initiative"] == {"epic-x": 1.5}
    assert got["today"]["by_role"] == {"build": 1.5}
    assert got["today"]["top_tasks"] == [{"task_id": "task-a", "cost_usd": 1.5}]

    assert got["week"]["total_usd"] == 3.5
    assert got["week"]["attributed_usd"] == 3.5
    assert got["week"]["unattributed_usd"] == 0.0
    assert got["week"]["by_initiative"] == {"epic-x": 1.5, "None": 2.0}
    assert got["week"]["top_tasks"] == [{"task_id": "task-b", "cost_usd": 2.0}, {"task_id": "task-a", "cost_usd": 1.5}]

    assert got["projection"] is None


def test_build_reports_the_unattributed_remainder_when_cost_since_and_usages_disagree(tmp_path: Path, monkeypatch):
    # `cost_since` sums `node_calls.cost_usd` directly; `usages` reads `r1.usage.json` instead of `r1`'s
    # `node_calls` rows because the file exists (per `usages`' own docstring). Here the file under-reports
    # what `node_calls` has for the same window, e.g. a usage file written before a later correction lands,
    # so the two independently-read totals disagree and the breakdown does not sum to the headline total.
    _cox_db(tmp_path, [(1.5, "2026-09-28T01:00:00+00:00"), (3.5, "2026-09-28T02:00:00+00:00")])
    _usage_file(
        tmp_path,
        "r1",
        [{"role": "build", "model": "sonnet", "task_id": "task-a", "cost_usd": 1.5, "ts": "2026-09-28T01:00:00+00:00"}],
    )
    monkeypatch.setattr(run_store, "work_items", lambda *a, **k: [{"task_id": "task-a", "initiative": "epic-x"}])
    monkeypatch.setattr(usage_meter, "read", lambda: None)

    got = build(tmp_path, "2026-09-28T12:00:00+00:00")

    assert got["today"]["total_usd"] == 5.0
    assert got["today"]["attributed_usd"] == 1.5
    assert got["today"]["unattributed_usd"] == 3.5
    assert got["today"]["by_initiative"] == {"epic-x": 1.5}


def test_build_projection_wires_a_populated_usage_meter_reading(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    resets_at = now + timedelta(hours=30)
    entry = MeterEntry(used_percentage=42.0, resets_at=resets_at)
    meter = Meter(five_hour=entry, seven_day=entry, observed_at=now)
    monkeypatch.setattr(usage_meter, "read", lambda: meter)
    monkeypatch.setattr(run_store, "work_items", lambda *a, **k: [])

    got = build(tmp_path, now.isoformat())

    # Ground truth for `ceiling_pct`/`used_pct`/`burn_pct_per_hour` comes from calling `usage_meter.as_window`
    # itself, not from re-deriving its arithmetic, so this checks wiring rather than duplicating the formula.
    expected_window = usage_meter.as_window(entry, now, timedelta(days=7))
    projection = got["projection"]
    assert projection["resets_at"] == resets_at.isoformat()
    assert projection["ceiling_pct"] == expected_window.ceiling_usd == 100.0
    assert projection["used_pct"] == expected_window.spent_usd == 42.0
    assert projection["burn_pct_per_hour"] == expected_window.burn_usd_per_hour

    hours_until_reset = (resets_at - now) / timedelta(hours=1)
    expected = project_to_hard_stop(
        expected_window.spent_usd, expected_window.ceiling_usd, expected_window.burn_usd_per_hour, hours_until_reset,
    )
    assert projection["remaining_pct"] == expected["remaining_pct"]
    assert projection["hours_to_exhaustion"] == expected["hours_to_exhaustion"]
    assert projection["hits_before_reset"] == expected["hits_before_reset"]
