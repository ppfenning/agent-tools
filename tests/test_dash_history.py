import json
from datetime import UTC, datetime, timedelta, timezone

from agent_tools.dash_history import build_history, history_today


def _task(run, task, **record):
    return {"run_id": run, "phase_id": "p1", "task_id": task, "record_json": json.dumps(record), "updated_at": "2026-10-04T10:00:00Z"}


def _run(run, ended_at, status="landed", host="box"):
    return {"run_id": run, "host": host, "status": status, "ended_at": ended_at}


def test_an_ended_run_with_a_landed_task_carries_its_pr_and_summed_cost():
    runs = [_run("acme-1", "2026-10-04T10:00:00Z")]
    tasks = [_task("acme-1", "t1", landed=True, ticket="t1", initiative="acme", pr="https://github.com/o/r/pull/7"), _task("acme-1", "t2", landed=False)]
    calls = [{"run_id": "acme-1", "cost_usd": 1.5}, {"run_id": "acme-1", "cost_usd": 0.25}, {"run_id": "other", "cost_usd": 9.0}]
    assert build_history(runs, tasks, calls) == [{
        "run": "acme-1", "machine": "box", "initiative": "acme", "ended_at": "2026-10-04T10:00:00Z",
        "outcome": "landed", "cost_usd": 1.75, "landed": [{"task": "t1", "pr": 7}],
    }]


def test_a_quarantined_run_carries_its_first_cause_and_others_carry_none():
    runs = [_run("a-1", "2026-10-04T10:00:00Z", status="quarantined"), _run("a-2", "2026-10-04T09:00:00Z", status="approved")]
    tasks = [
        _task("a-1", "t1", attempts=[{"kind": "quarantine", "cause": "harness"}, {"cause": "code"}]),
        _task("a-1", "t2", attempts=[{"cause": "ticket"}]),
    ]
    rows = build_history(runs, tasks, [])
    assert rows[0]["outcome"] == "quarantined" and rows[0]["cause"] == "harness" and rows[0]["cost_usd"] == 0.0
    assert rows[1]["outcome"] == "approved" and "cause" not in rows[1]


def test_a_run_with_no_end_is_never_in_the_history():
    runs = [_run("a-1", None, status="running"), _run("a-2", "", status="running")]
    assert build_history(runs, [], []) == []


def test_history_is_newest_ended_first_and_cut_to_the_limit():
    runs = [_run("a-1", "2026-10-04T08:00:00Z"), _run("a-3", "2026-10-04T10:00:00Z"), _run("a-2", "2026-10-04T09:00:00Z")]
    assert [row["run"] for row in build_history(runs, [], [], limit=2)] == ["a-3", "a-2"]


def test_an_unlanded_run_with_no_known_status_is_stopped_or_crashed():
    runs = [_run("a-1", "2026-10-04T10:00:00Z", status="budget"), _run("a-2", "2026-10-04T09:00:00Z", status="exited")]
    assert [row["outcome"] for row in build_history(runs, [], [])] == ["stopped", "crashed"]


def test_todays_counts_exclude_a_run_ended_before_local_midnight():
    tz = timezone(timedelta(hours=-7))
    now = datetime(2026, 10, 4, 20, 0, tzinfo=UTC)
    runs = [
        _run("a-1", "2026-10-04T18:00:00Z"),
        _run("a-2", "2026-10-04T12:00:00Z", status="quarantined"),
        _run("a-3", "2026-10-04T05:00:00Z"),
        _run("a-4", None, status="running"),
    ]
    calls = [{"run_id": "a-1", "cost_usd": 2.0}, {"run_id": "a-2", "cost_usd": 0.5}, {"run_id": "a-3", "cost_usd": 7.0}]
    assert history_today(runs, [], calls, now, tz) == {"lands": 1, "quarantines": 1, "cost_usd": 2.5, "runs": 2}
