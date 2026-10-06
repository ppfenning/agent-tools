from datetime import UTC, datetime

from agent_tools import run_store, usage_window


def test_store_only_usage_reads_start_times_with_one_store_call(tmp_path, monkeypatch):
    # 445 per-run connections made `cox route context` take 24 s (2026-10-06); the start times come from one query now.
    calls = []
    monkeypatch.setattr(run_store, "store_usages", lambda root, exclude=(), since=None: {"a-1": {"cost": 1}, "b-2": {"cost": 2}})
    monkeypatch.setattr(run_store, "runs_started", lambda root: calls.append(root) or {"a-1": "2026-10-06T01:00:00+00:00"})

    def per_run(*args):
        raise AssertionError("no per-run lookup")

    monkeypatch.setattr(run_store, "run_started", per_run)
    got = usage_window._read_usage_files(tmp_path, datetime(2026, 10, 6, 2, tzinfo=UTC))
    assert len(calls) == 1
    assert [(started.isoformat(), usage) for started, usage in got] == [("2026-10-06T01:00:00+00:00", {"cost": 1})]
