from agent_tools.chair_revert_exec import RevertResult, perform_revert
from agent_tools.land import pr_footer

URL = "https://example.test/pull/9"


class FakeGit:
    def __init__(self, calls: list, conflicts: list[str]) -> None:
        self.calls, self.conflicts = calls, conflicts

    def fetch_main(self, repo: str) -> None:
        self.calls.append(("fetch", repo))

    def revert_onto_branch(self, repo: str, branch: str, commit: str) -> list[str]:
        self.calls.append(("revert", repo, branch, commit))
        return self.conflicts

    def push(self, repo: str, branch: str) -> None:
        self.calls.append(("push", repo, branch))


class FakeForge:
    def __init__(self, calls: list, failing: list[str]) -> None:
        self.calls, self.failing = calls, failing

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        self.calls.append(("open", repo, branch, title, body))
        return URL

    def wait_checks(self, pr: str) -> list[str]:
        self.calls.append(("wait", pr))
        return self.failing

    def merge(self, pr: str) -> None:
        self.calls.append(("merge", pr))


def run(conflicts: list[str], failing: list[str]) -> tuple[RevertResult, list]:
    calls: list = []
    result = perform_revert("r", 7, "abc123", "tests red", FakeGit(calls, conflicts), FakeForge(calls, failing))
    return result, calls


def test_clean_revert_runs_in_order_and_merges() -> None:
    result, calls = run([], [])
    assert [c[0] for c in calls] == ["fetch", "revert", "push", "open", "wait", "merge"]
    assert result == RevertResult("merged", URL, "")
    assert calls[1] == ("revert", "r", "revert/7", "abc123")
    assert calls[2] == ("push", "r", "revert/7")


def test_conflict_stops_before_push_and_pr() -> None:
    result, calls = run(["a.py", "b.py"], [])
    assert [c[0] for c in calls] == ["fetch", "revert"]
    assert result.status == "conflict"
    assert result.pr == ""
    assert "a.py" in result.detail and "b.py" in result.detail


def test_failing_checks_leave_pr_unmerged() -> None:
    result, calls = run([], ["lint"])
    assert [c[0] for c in calls] == ["fetch", "revert", "push", "open", "wait"]
    assert result.status == "checks_failed"
    assert result.pr == URL
    assert "lint" in result.detail


def test_title_names_the_pr_number() -> None:
    _, calls = run([], [])
    assert calls[3][3] == "Revert #7: main red after land"


def test_body_carries_reason_and_ends_with_footer() -> None:
    _, calls = run([], [])
    body = calls[3][4]
    assert "tests red" in body
    assert body.endswith(pr_footer(None))
