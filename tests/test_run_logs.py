import gzip
import os
from datetime import UTC, datetime

import pytest

from agent_tools import route, run_logs

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
OLD = datetime(2026, 9, 18, 12, 0, tzinfo=UTC).timestamp()
NEW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC).timestamp()


def test_a_log_and_a_calls_log_belong_to_their_run_and_nothing_else_does():
    assert (run_logs.run_of("x-2.log"), run_logs.run_of("x-2.calls.jsonl"), run_logs.run_of("x-2.pid")) == ("x-2", "x-2", None)


def test_only_an_ended_runs_logs_older_than_the_retention_are_due():
    files = [("a-1.log", OLD), ("a-1.calls.jsonl", OLD), ("b-1.log", OLD), ("c-1.log", NEW)]
    assert run_logs.due(files, {"a-1", "c-1"}, NOW, 7) == ["a-1.calls.jsonl", "a-1.log"]


def test_retention_defaults_to_7_days_and_reads_the_profile_key():
    assert [run_logs.retention_days(p) for p in ({}, {"log_retention_days": "14"}, {"log_retention_days": "0"}, {"log_retention_days": "x"})] == [7, 14, 7, 7]


def test_the_routing_profile_accepts_log_retention_days():
    assert route.parse_profile("workspace_dir: /w\nlog_retention_days: 10\n")["log_retention_days"] == "10"


def test_logs_are_archived_beside_the_traces_by_month():
    assert run_logs.logs_root("s3://coxswain/traces") == "s3://coxswain/logs"
    assert run_logs.object_path("s3://coxswain/logs", "a-1.log", OLD) == "s3://coxswain/logs/2026/09/a-1.log.gz"


def test_a_due_log_is_archived_readably_and_then_removed_locally(tmp_path):
    load_file_io = pytest.importorskip("pyiceberg.io").load_file_io  # the `lake` extra; the extras job runs this

    runs = tmp_path / "runs"
    runs.mkdir()
    log = runs / "a-1.log"
    log.write_text("hello\n")
    os.utime(log, (OLD, OLD))
    keep = runs / "b-1.log"
    keep.write_text("live\n")
    root = str(tmp_path / "logs")
    outcome = run_logs.archive_and_prune(runs, root, load_file_io({}, root), {"a-1"}, NOW, 7)
    assert (outcome.due, outcome.archived, outcome.failed, log.exists(), keep.exists()) == (1, 1, [], False, True)
    assert gzip.decompress((tmp_path / "logs" / "2026" / "09" / "a-1.log.gz").read_bytes()) == b"hello\n"


def test_a_failed_archive_keeps_the_local_log(tmp_path):
    class Broken:
        def new_output(self, path):
            raise OSError("store unreachable")

    runs = tmp_path / "runs"
    runs.mkdir()
    log = runs / "a-1.log"
    log.write_text("hello\n")
    os.utime(log, (OLD, OLD))
    outcome = run_logs.archive_and_prune(runs, "s3://x/logs", Broken(), {"a-1"}, NOW, 7)
    assert (outcome.archived, len(outcome.failed), log.exists()) == (0, 1, True)
