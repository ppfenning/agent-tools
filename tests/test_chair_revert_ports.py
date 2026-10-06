from __future__ import annotations

import json

import pytest

from agent_tools.chair_revert_ports import GhForgePort, GitRevertPort, main_ci, parent_count

G = ["git", "-C", "/r"]
PROBE = [*G, "rev-list", "--parents", "-n", "1", "abc"]
CHECKOUT = [*G, "checkout", "-b", "revert/7", "origin/main"]


class Recorder:
    """Runner that records argv and answers from a script keyed by an argv prefix."""

    def __init__(self, script: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.script = script or {}

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        for prefix, answer in self.script.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return answer
        return 0, ""


def test_parent_count_reads_the_rev_list_line() -> None:
    assert (parent_count("abc p1"), parent_count("abc p1 p2")) == (1, 2)


def test_fetch_main_argv() -> None:
    run = Recorder()
    GitRevertPort(run).fetch_main("/r")
    assert run.calls == [[*G, "fetch", "origin", "main"]]


def test_clean_revert_issues_checkout_revert_push_in_order() -> None:
    run = Recorder({("git", "-C", "/r", "rev-list"): (0, "abc p1\n")})
    port = GitRevertPort(run)
    assert port.revert_onto_branch("/r", "revert/7", "abc") == []
    port.push("/r", "revert/7")
    assert run.calls == [
        PROBE,
        CHECKOUT,
        [*G, "revert", "--no-edit", "abc"],
        [*G, "push", "origin", "revert/7"],
    ]


def test_two_parent_commit_adds_m_1() -> None:
    run = Recorder({("git", "-C", "/r", "rev-list"): (0, "abc p1 p2\n")})
    GitRevertPort(run).revert_onto_branch("/r", "revert/7", "abc")
    assert run.calls[-1] == [*G, "revert", "--no-edit", "-m", "1", "abc"]


def test_conflict_returns_unmerged_files_and_aborts() -> None:
    run = Recorder(
        {
            ("git", "-C", "/r", "rev-list"): (0, "abc p1\n"),
            ("git", "-C", "/r", "revert", "--no-edit"): (1, "CONFLICT"),
            ("git", "-C", "/r", "diff"): (0, "a.py\nb/c.py\n"),
        }
    )
    assert GitRevertPort(run).revert_onto_branch("/r", "revert/7", "abc") == ["a.py", "b/c.py"]
    assert run.calls[-4:] == [
        [*G, "diff", "--name-only", "--diff-filter=U"],
        [*G, "revert", "--abort"],
        [*G, "checkout", "--detach", "origin/main"],
        [*G, "branch", "-D", "revert/7"],
    ]


def test_failed_revert_without_conflicts_raises() -> None:
    run = Recorder(
        {("git", "-C", "/r", "rev-list"): (0, "abc p1\n"), ("git", "-C", "/r", "revert", "--no-edit"): (1, "empty")}
    )
    with pytest.raises(RuntimeError, match="no conflicts"):
        GitRevertPort(run).revert_onto_branch("/r", "revert/7", "abc")


def test_push_failure_raises() -> None:
    run = Recorder({("git",): (1, "rejected")})
    with pytest.raises(RuntimeError, match="rejected"):
        GitRevertPort(run).push("/r", "revert/7")


def test_open_pr_returns_the_url() -> None:
    run = Recorder({("gh",): (0, "Creating pull request\nhttps://github.com/o/r/pull/9\n")})
    url = GhForgePort(run).open_pr("o/r", "revert/7", "Revert #7", "why")
    assert url == "https://github.com/o/r/pull/9"
    assert run.calls == [
        ["gh", "pr", "create", "--repo", "o/r", "--head", "revert/7", "--title", "Revert #7", "--body", "why"]
    ]


def test_wait_checks_returns_only_failing_names() -> None:
    out = "lint\tpass\t10s\thttp://x\ntests\tfail\t1m\thttp://y\nbuild\tpending\t0\thttp://z\n"
    run = Recorder({("gh",): (1, out)})
    assert GhForgePort(run).wait_checks("https://github.com/o/r/pull/9") == ["tests"]
    assert run.calls == [["gh", "pr", "checks", "https://github.com/o/r/pull/9", "--watch"]]


def test_wait_checks_raises_without_evidence() -> None:
    with pytest.raises(RuntimeError, match="no failing check"):
        GhForgePort(Recorder({("gh",): (1, "")})).wait_checks("https://github.com/o/r/pull/9")


def test_merge_squashes() -> None:
    run = Recorder()
    GhForgePort(run).merge("https://github.com/o/r/pull/9")
    assert run.calls == [["gh", "pr", "merge", "https://github.com/o/r/pull/9", "--squash"]]


def _listing(status: str, conclusion: str) -> dict[tuple[str, ...], tuple[int, str]]:
    runs = [{"databaseId": 55, "status": status, "conclusion": conclusion}]
    return {("gh", "run", "list"): (0, json.dumps(runs))}


def test_main_ci_in_progress_is_pending() -> None:
    assert main_ci(Recorder(_listing("in_progress", "")), "o/r", "abc") == ("pending", "")


def test_main_ci_success_is_green() -> None:
    assert main_ci(Recorder(_listing("completed", "success")), "o/r", "abc") == ("green", "")


def test_main_ci_failure_is_red_with_log_tail() -> None:
    log = "\n".join(f"line {n}" for n in range(100))
    run = Recorder({**_listing("completed", "failure"), ("gh", "run", "view"): (0, log)})
    state, output = main_ci(run, "o/r", "abc")
    assert (state, output.splitlines()[0], output.splitlines()[-1]) == ("red", "line 60", "line 99")
    assert run.calls[0] == [
        "gh", "run", "list", "--repo", "o/r", "--branch", "main", "--commit", "abc",
        "--json", "databaseId,status,conclusion", "--limit", "1",
    ]  # fmt: skip
    assert run.calls[1] == ["gh", "run", "view", "55", "--repo", "o/r", "--log-failed"]


def test_main_ci_cancelled_is_not_red() -> None:
    assert main_ci(Recorder(_listing("completed", "cancelled")), "o/r", "abc") == ("pending", "run concluded cancelled")


def test_main_ci_unreadable_log_is_labelled() -> None:
    run = Recorder({**_listing("completed", "failure"), ("gh", "run", "view"): (1, "HTTP 401")})
    assert main_ci(run, "o/r", "abc") == ("red", "failing log unavailable, gh run view exited 1: HTTP 401")


def test_main_ci_no_run_yet_is_pending() -> None:
    assert main_ci(Recorder({("gh",): (0, "[]")}), "o/r", "abc") == ("pending", "")
