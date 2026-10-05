from agent_tools.chair_read_review_prs import awaiting_reviews

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


def test_a_url_absent_from_states_yields_state_unknown() -> None:
    assert awaiting_reviews([_rec(review_pr=_URL)], {})[0]["state"] == "unknown"
