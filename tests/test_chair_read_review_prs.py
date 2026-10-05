import json
from collections.abc import Sequence
from pathlib import Path

from agent_tools.chair_read_review_prs import awaiting_reviews, read_review_prs

_URL = "https://github.com/o/r/pull/7"


def _rec(**over: object) -> dict:
    return {"initiative": "init", "phase": "build", "task_id": "a", "repo": "/r/x", "status": "approved", **over}


def test_an_approved_record_with_a_review_pr_is_returned_with_the_given_state() -> None:
    states = {_URL: {"state": "merged", "merged_at": "2026-10-04T01:00:00Z"}}
    assert awaiting_reviews([_rec(review_pr=_URL)], states) == [
        {"initiative": "init", "phase": "build", "task_id": "a", "repo": "/r/x", "url": _URL, "state": "merged", "merged_at": "2026-10-04T01:00:00Z"}
    ]


def test_a_record_without_a_review_pr_is_absent() -> None:
    assert awaiting_reviews([_rec()], {}) == []


def test_a_done_record_with_a_review_pr_is_absent() -> None:
    assert awaiting_reviews([_rec(status="done", review_pr=_URL)], {_URL: {"state": "open", "merged_at": None}}) == []


def test_the_row_carries_the_owning_run_and_its_host_from_the_store_row(tmp_path) -> None:
    task = tmp_path / "init-1" / "tasks" / "build"
    task.mkdir(parents=True)
    (task / "a.json").write_text(json.dumps({"status": "approved", "review_pr": _URL, "repo": "/r/x"}), encoding="utf-8")
    store_rows = {"init-1": {"run_id": "init-1", "host": "lane-a"}}  # the fake runs-table row

    def run_hosts(runs_dir: Path, runs: Sequence[str]) -> dict[str, str]:
        return {run: store_rows[run]["host"] for run in runs if run in store_rows}

    rows = read_review_prs(tmp_path, lambda url: {"state": "merged", "merged_at": None}, run_hosts)
    assert (rows[0]["run"], rows[0]["host"]) == ("init-1", "lane-a")


def test_a_url_absent_from_states_yields_state_unknown() -> None:
    assert awaiting_reviews([_rec(review_pr=_URL)], {})[0]["state"] == "unknown"
