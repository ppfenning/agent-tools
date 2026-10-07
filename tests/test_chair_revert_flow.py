from dataclasses import replace

from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_facts import gather_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import LandWatch
from tests.test_chair_facts import NOW
from tests.test_chair_facts import _deps as _facts_deps

WATCH: LandWatch = {"initiative": "i", "phase": "p1", "repo": "/r", "pr": 7, "commit": "abc"}
EPOCH = 4  # the lease epoch `tests.test_chair_facts._deps` plans under
PR_URL = "https://forge.test/r/pulls/9"
MAIN_RED_ATTEMPT = {"run": "i-1", "phase": "p1", "task": "a", "cause": "main_red"}


class FakeGit:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def fetch_main(self, repo: str) -> None:
        self.calls.append(("fetch_main", repo))

    def revert_onto_branch(self, repo: str, branch: str, commit: str) -> list[str]:
        self.calls.append(("revert_onto_branch", repo, branch, commit))
        return []

    def push(self, repo: str, branch: str) -> None:
        self.calls.append(("push", repo, branch))


class FakeForge:
    def __init__(self, calls: list, failing: list[str]) -> None:
        self.calls = calls
        self.failing = failing

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        self.calls.append(("open_pr", repo, branch))
        return PR_URL

    def wait_checks(self, pr: str) -> list[str]:
        self.calls.append(("wait_checks", pr))
        return self.failing

    def merge(self, pr: str) -> None:
        self.calls.append(("merge", pr))


def _plan(ci: tuple[str, str], outcomes: dict[str, list[str]] | None = None, attempts: tuple[dict, ...] = ()):
    facts_deps = replace(
        _facts_deps(attempts=attempts, quarantined=() if not attempts else ({"initiative": "i", "phase": "p1", "task": "a"},), stranded=()),
        land_watches=lambda: [WATCH],
        read_main_ci=lambda repo, commit: ci,
        land_hold=lambda: None,
        land_outcomes=lambda: outcomes or {},
        run_exited=lambda: {"i": True},
    )
    return plan_tick(gather_facts(facts_deps, NOW), NOW)


def _perform(actions: list, calls: list, failing: list[str] | None = None) -> list:
    git, forge = FakeGit(calls), FakeForge(calls, failing or [])
    deps = replace(
        Deps(
            run=lambda argv: calls.append(("run", argv)) or (0, ""),
            delete_branches=lambda repo, pattern: calls.append(("delete", repo, pattern)) or ([], ""),
            acquire_lease=lambda holder, host: calls.append(("lease", holder)) or "",
            record=lambda action: calls.append(("record", action["kind"])),
            run_id=lambda action: "run-1",
            repo_for=lambda action: action.get("repo", "r"),
        ),
        revert_ports=lambda action: (git, forge),
        quarantine_phase=lambda initiative, phase, cause, reason: calls.append(("quarantine", initiative, phase, cause, reason)),
        resolve_land=lambda initiative, phase, outcome: calls.append(("resolve", initiative, phase, outcome)),
    )
    return perform(actions, deps, lambda: EPOCH, False)


def _kinds(calls: list) -> list[str]:
    return [c[0] for c in calls]


def _revert_results(results: list) -> list:
    return [r for r in results if r["action"]["kind"] == "revert_land"]


def test_a_red_main_after_a_land_is_reverted_quarantined_and_resolved():
    actions = _plan(("red", "boom"))
    reverts = [a for a in actions if a["kind"] == "revert_land"]
    assert len(reverts) == 1
    assert (reverts[0]["initiative"], reverts[0]["phase"], reverts[0]["pr"], reverts[0]["commit"]) == ("i", "p1", 7, "abc")

    calls: list = []
    results = _perform(reverts, calls)

    assert [c for c in calls if c[0] not in ("record",)] == [
        ("fetch_main", "/r"),
        ("revert_onto_branch", "/r", "revert/7", "abc"),
        ("push", "/r", "revert/7"),
        ("open_pr", "/r", "revert/7"),
        ("wait_checks", PR_URL),
        ("merge", PR_URL),
        ("quarantine", "i", "p1", "main_red", "boom"),
        ("resolve", "i", "p1", "reverted"),
    ]
    [result] = _revert_results(results)
    assert result["status"] == "done"
    assert result["needs_chair"]["cause"] == "main_red"


def test_a_green_main_plans_no_revert_and_touches_nothing():
    actions = _plan(("green", ""))
    assert [a for a in actions if a["kind"] == "revert_land"] == []

    calls: list = []
    _perform([a for a in actions if a["kind"] == "revert_land"], calls)

    assert [c for c in calls if c[0] in ("open_pr", "merge", "quarantine", "resolve")] == []


def test_a_revert_whose_checks_fail_is_not_merged_and_leaves_the_land_pending():
    reverts = [a for a in _plan(("red", "boom")) if a["kind"] == "revert_land"]

    calls: list = []
    results = _perform(reverts, calls, failing=["tests"])

    assert "merge" not in _kinds(calls)
    assert ("quarantine", "i", "p1", "main_red", "boom") in calls
    assert "resolve" not in _kinds(calls)
    [result] = _revert_results(results)
    assert result["status"] == "failed"
    assert result["needs_chair"]["cause"] == "revert_failed"


def test_two_consecutive_reverts_stop_the_relaunch_and_one_does_not():
    attempts = (MAIN_RED_ATTEMPT,)
    stopped = _plan(("green", ""), {"i": ["reverted", "reverted"]}, attempts)
    once = _plan(("green", ""), {"i": ["reverted"]}, attempts)

    assert [a for a in stopped if a["kind"] == "relaunch" and a.get("initiative") == "i"] == []
    assert [a for a in once if a["kind"] == "relaunch" and a.get("initiative") == "i"] != []
