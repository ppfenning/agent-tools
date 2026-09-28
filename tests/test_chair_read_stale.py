import os
import sqlite3
import subprocess
from datetime import UTC, datetime

import pytest

from agent_tools.chair_read_quarantined import body_sha
from agent_tools.chair_read_stale import (
    file_changed_at,
    last_chair_action_at,
    last_run_at,
    quarantine_non_harness_count,
    read_chair_actions,
    read_stale_candidates,
)

BODY = "the ticket body\n"
SHA = body_sha(BODY)
KEY = ("a", "p1", "t1")


def test_last_run_at_is_the_newest_ts_of_the_tasks_own_attempts():
    attempts = [
        {"initiative": "a", "phase": "p1", "task": "t1", "ts": "2026-09-20T00:00:00Z"},
        {"initiative": "a", "phase": "p1", "task": "t1", "ts": "2026-09-22T00:00:00Z"},
        {"initiative": "a", "phase": "p1", "task": "t2", "ts": "2026-09-25T00:00:00Z"},
    ]
    assert last_run_at(attempts, KEY) == "2026-09-22T00:00:00Z"


def test_last_run_at_with_no_attempts_for_the_task_is_none():
    assert last_run_at([{"initiative": "a", "phase": "p1", "task": "t2", "ts": "2026-09-25T00:00:00Z"}], KEY) is None


def test_last_run_at_ignores_the_same_task_id_in_another_phase():
    assert last_run_at([{"initiative": "a", "phase": "p2", "task": "t1", "ts": "2026-09-25T00:00:00Z"}], KEY) is None


def test_quarantine_non_harness_count_is_two_for_two_non_harness_and_one_harness_attempt_on_the_current_body():
    attempts = [
        {"initiative": "a", "phase": "p1", "task": "t1", "cause": "assertion", "body_sha": SHA},
        {"initiative": "a", "phase": "p1", "task": "t1", "cause": "timeout", "body_sha": SHA},
        {"initiative": "a", "phase": "p1", "task": "t1", "cause": "harness", "body_sha": SHA},
    ]
    assert quarantine_non_harness_count(attempts, BODY, KEY) == 2


def test_quarantine_non_harness_count_skips_an_earlier_body_and_another_phase():
    attempts = [
        {"initiative": "a", "phase": "p1", "task": "t1", "cause": "assertion", "body_sha": "0" * 12},
        {"initiative": "a", "phase": "p2", "task": "t1", "cause": "assertion", "body_sha": SHA},
    ]
    assert quarantine_non_harness_count(attempts, BODY, KEY) == 0


def test_last_chair_action_at_prefers_a_task_row_over_a_newer_initiative_row():
    actions = [{"target": "a", "ts": "2026-09-26T00:00:00Z"}, {"target": "t1", "ts": "2026-09-20T00:00:00Z"}]
    assert last_chair_action_at(actions, KEY) == "2026-09-20T00:00:00Z"


def test_last_chair_action_at_falls_back_to_the_initiative_row():
    assert last_chair_action_at([{"target": "a", "ts": "2026-09-26T00:00:00Z"}], KEY) == "2026-09-26T00:00:00Z"


def test_last_chair_action_at_skips_a_task_row_that_names_another_phase_or_initiative():
    actions = [
        {"target": "t1", "phase": "p2", "ts": "2026-09-26T00:00:00Z"},
        {"target": "t1", "initiative": "b", "ts": "2026-09-27T00:00:00Z"},
    ]
    assert last_chair_action_at(actions, KEY) is None


def _store(tmp_path, columns):
    runs = tmp_path / "runs"
    runs.mkdir()
    conn = sqlite3.connect(runs / "cox.db")
    with conn:
        conn.execute(f"CREATE TABLE chair_actions ({', '.join(columns)})")
        conn.execute(f"INSERT INTO chair_actions VALUES ({', '.join('?' * len(columns))})", ["x"] * len(columns))
    conn.close()
    return runs


def test_read_chair_actions_reads_ts_and_target_from_a_fixture_store(tmp_path):
    rows = read_chair_actions(_store(tmp_path, ["ts", "kind", "target"]))
    assert rows == [{"ts": "x", "kind": "x", "target": "x"}]


def test_read_chair_actions_raises_on_a_table_with_no_target_column(tmp_path):
    with pytest.raises(RuntimeError, match="no target column"):
        read_chair_actions(_store(tmp_path, ["ts", "kind"]))


def test_read_chair_actions_with_no_store_is_empty(tmp_path):
    assert read_chair_actions(tmp_path / "runs") == []


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)


def _git_repo_with_committed_file(tmp_path):
    repo = tmp_path / "workspace"
    (repo / "work" / "a" / "p1").mkdir(parents=True)
    (repo / "work" / "a" / "p1" / "t1.md").write_text("---\nstate: ready\n---\nhi\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    date = "2026-09-20T10:00:00+02:00"
    identity = {"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b.c", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@b.c"}
    _git(repo, "commit", "-q", "-m", "x", env={**os.environ, **identity, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return repo


def test_file_changed_at_of_a_git_tracked_file_is_its_commit_instant(tmp_path):
    # %aI keeps a non-UTC offset as written; git versions differ on whether UTC prints as `Z` or `+00:00`.
    changed = file_changed_at(_git_repo_with_committed_file(tmp_path), "a", "p1", "t1")
    assert datetime.fromisoformat(changed) == datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def test_file_changed_at_of_a_file_with_no_commit_history_is_none(tmp_path):
    assert file_changed_at(_git_repo_with_committed_file(tmp_path), "a", "p1", "never-committed") is None


def _write_task(repo, phase, task, state, attempts=""):
    (repo / "work" / "a" / phase).mkdir(parents=True, exist_ok=True)
    (repo / "work" / "a" / phase / f"{task}.md").write_text(f"---\nstate: {state}\n{attempts}---\n{BODY}")


def test_read_stale_candidates_gives_one_row_per_open_task_and_keeps_phases_apart(tmp_path):
    repo = tmp_path / "workspace"
    attempts = "".join(
        f"  - run: a-{n}\n    ts: 2026-09-2{n}T00:00:00Z\n    cause: {cause}\n    body_sha: {SHA}\n"
        for n, cause in ((1, "assertion"), (2, "timeout"), (3, "harness"))
    )
    _write_task(repo, "p1", "t1", "ready", f"attempts:\n{attempts}")
    _write_task(repo, "p2", "t1", "blocked")
    _write_task(repo, "p1", "done-task", "done")
    rows = read_stale_candidates(repo, datetime(2026, 9, 27, tzinfo=UTC))
    blank = {"initiative": "a", "task_id": "t1", "last_file_change": None, "last_chair_action": None}
    assert rows == [
        {**blank, "state": "ready", "last_run": "2026-09-23T00:00:00+00:00", "quarantine_non_harness_count": 2},
        {**blank, "state": "blocked", "last_run": None, "quarantine_non_harness_count": 0},
    ]
