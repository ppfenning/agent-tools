from __future__ import annotations

from agent_tools import chair_carry_exec
from agent_tools.chair_types import Action, CarryTask
from agent_tools.land import pr_footer


class Fake:
    """One object standing in for all three ports; every call lands in `calls` in order."""

    def __init__(
        self,
        conflicts: dict[str, list[str]] | None = None,
        repo_failing: list[str] | None = None,
        pr_failing: list[str] | None = None,
        on_main: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.bodies: list[str] = []
        self.conflicts = conflicts or {}
        self.repo_failing = repo_failing or []
        self.pr_failing = pr_failing or []
        self.on_main = on_main or set()

    def fetch_branch(self, host: str, branch: str) -> None:
        self.calls.append(("fetch", host, branch))

    def create_branch_from_main(self, name: str) -> None:
        self.calls.append(("branch", name))

    def cherry_pick(self, commit: str) -> list[str]:
        self.calls.append(("pick", commit))
        return self.conflicts.get(commit, [])

    def push(self, branch: str) -> None:
        self.calls.append(("push", branch))

    def run_checks(self) -> list[str]:
        self.calls.append(("checks",))
        return self.repo_failing

    def already_on_main(self, commit: str) -> bool:
        return commit in self.on_main

    def open_pr(self, branch: str, title: str, body: str) -> str:
        self.calls.append(("pr", branch))
        self.bodies.append(body)
        return "pr-1"

    def wait_checks(self, pr: str) -> list[str]:
        self.calls.append(("wait", pr))
        return self.pr_failing

    def merge(self, pr: str) -> None:
        self.calls.append(("merge", pr))

    def mark_landed(self, task: str, run: str) -> None:
        self.calls.append(("landed", task, run))

    def set_done(self, task: str) -> None:
        self.calls.append(("done", task))


def pick(task: str, run: str = "r1", host: str = "h1", branch: str = "agents/r1", commit: str = "") -> CarryTask:
    return {"task": task, "run": run, "host": host, "branch": branch, "commit": commit or f"c-{task}", "needs": [], "run_seq": 1}


def carry(*picks: CarryTask) -> Action:
    return {"kind": "carry_phase", "initiative": "init", "phase": "p2", "pr_branch": "pr/carry-init-p2", "picks": list(picks)}


def run(action: Action, fake: Fake):
    return chair_carry_exec.perform_carry(action, fake, fake, fake)


def test_stranded_phase_runs_in_order_and_marks_after_merge() -> None:
    fake = Fake()
    result = run(carry(pick("t1"), pick("t2")), fake)
    assert fake.calls == [
        ("fetch", "h1", "agents/r1"), ("branch", "pr/carry-init-p2"), ("pick", "c-t1"), ("pick", "c-t2"),
        ("checks",), ("push", "pr/carry-init-p2"), ("pr", "pr/carry-init-p2"), ("wait", "pr-1"), ("merge", "pr-1"),
        ("landed", "t1", "r1"), ("done", "t1"), ("landed", "t2", "r1"), ("done", "t2"),
    ]
    assert result["status"] == "done"


def test_two_hosts_fetch_each_once_and_open_one_pr() -> None:
    fake = Fake()
    run(carry(pick("t1"), pick("t2", run="r2", host="h2", branch="agents/r2"), pick("t3", commit="c-t3b")), fake)
    assert [c for c in fake.calls if c[0] == "fetch"] == [("fetch", "h1", "agents/r1"), ("fetch", "h2", "agents/r2")]
    assert [c for c in fake.calls if c[0] == "pr"] == [("pr", "pr/carry-init-p2")]


def test_conflict_on_second_pick_stops_with_nothing_marked() -> None:
    fake = Fake(conflicts={"c-t2": ["b.py", "a.py"]})
    result = run(carry(pick("t1"), pick("t2")), fake)
    assert result["action"]["kind"] == "needs_chair"
    assert result["action"]["cause"] == "carry_conflict"
    assert "a.py, b.py" in result["action"]["reason"]
    assert [c[0] for c in fake.calls] == ["fetch", "branch", "pick", "pick"]


def test_failing_repository_checks_stop_before_push() -> None:
    fake = Fake(repo_failing=["pytest"])
    result = run(carry(pick("t1")), fake)
    assert result["action"]["cause"] == "carry_checks_failed"
    assert [c[0] for c in fake.calls] == ["fetch", "branch", "pick", "checks"]


def test_failing_pr_checks_stop_before_merge_and_mark_nothing() -> None:
    fake = Fake(pr_failing=["ci"])
    result = run(carry(pick("t1")), fake)
    assert result["action"]["cause"] == "carry_checks_failed"
    assert [c[0] for c in fake.calls] == ["fetch", "branch", "pick", "checks", "push", "pr", "wait"]


def test_pick_already_on_main_is_skipped() -> None:
    fake = Fake(on_main={"c-t1"})
    run(carry(pick("t1"), pick("t2")), fake)
    assert [c for c in fake.calls if c[0] == "pick"] == [("pick", "c-t2")]
    assert ("landed", "t1", "r1") in fake.calls


def test_all_picks_on_main_opens_no_pr_and_marks_them() -> None:
    fake = Fake(on_main={"c-t1", "c-t2"})
    result = run(carry(pick("t1"), pick("t2")), fake)
    assert fake.calls == [
        ("fetch", "h1", "agents/r1"), ("branch", "pr/carry-init-p2"),
        ("landed", "t1", "r1"), ("done", "t1"), ("landed", "t2", "r1"), ("done", "t2"),
    ]
    assert result["status"] == "done"


def test_pr_body_ends_with_footer() -> None:
    fake = Fake()
    run(carry(pick("t1")), fake)
    assert fake.bodies[0].endswith(pr_footer(None))
