from dataclasses import replace

from agent_tools.chair_facts import gather_facts, phase_branch_facts, stranded_facts
from tests.test_chair_facts import NOW, _deps


def _approved(task: str, needs: list[str] | None = None) -> dict:
    return {
        "id": task,
        "initiative": "i",
        "repo": "/r",
        "phase": "p1",
        "phase_done": False,
        "needs": needs or [],
        "run": "r1",
        "needs_fetch": False,
    }


def _commit(task: str, run: str, seq: int, sha: str) -> dict:
    return {
        "task": task,
        "initiative": "i",
        "phase": "p1",
        "run": run,
        "host": f"h{seq}",
        "branch": f"agents/{run}",
        "commit": sha,
        "run_seq": seq,
    }


def test_task_approved_in_two_runs_yields_two_rows_with_run_seq():
    phases = [{"initiative": "i", "phase": "p1", "tasks": ["a"], "adds": True}]
    commits = [_commit("a", "r1", 1, "c1"), _commit("a", "r1", 1, "c2"), _commit("a", "r2", 2, "c3")]
    out = stranded_facts([_approved("a", ["z"])], commits, phases)
    assert out == [
        {
            "initiative": "i",
            "phase": "p1",
            "phase_branch": "epic/i/p1",
            "approved": [
                {
                    "task": "a",
                    "run": "r1",
                    "host": "h1",
                    "branch": "agents/r1",
                    "commit": "c2",
                    "needs": ["z"],
                    "run_seq": 1,
                },
                {
                    "task": "a",
                    "run": "r2",
                    "host": "h2",
                    "branch": "agents/r2",
                    "commit": "c3",
                    "needs": ["z"],
                    "run_seq": 2,
                },
            ],
            "pending": [],
        }
    ]


def test_fully_landed_phase_yields_none():
    phases = [{"initiative": "i", "phase": "p1", "tasks": ["a"], "adds": False}]
    assert stranded_facts([], [_commit("a", "r1", 1, "c1")], phases) == []


def test_approved_phase_in_one_run_that_adds_over_main_yields_none():
    phases = [{"initiative": "i", "phase": "p1", "tasks": ["a"], "adds": True}]
    assert stranded_facts([_approved("a")], [_commit("a", "r1", 1, "c1")], phases) == []


def test_approved_phase_in_one_run_that_adds_nothing_over_main_is_stranded():
    phases = [{"initiative": "i", "phase": "p1", "tasks": ["a"], "adds": False}]
    out = stranded_facts([_approved("a")], [_commit("a", "r1", 1, "c1")], phases)
    assert [(p["phase"], [r["run"] for r in p["approved"]]) for p in out] == [("p1", ["r1"])]


def test_a_commit_for_a_task_that_is_not_approved_gets_no_row():
    phases = [{"initiative": "i", "phase": "p1", "tasks": ["a", "b"], "adds": False}]
    commits = [_commit("a", "r1", 1, "c1"), _commit("b", "r2", 2, "c2")]
    out = stranded_facts([_approved("a")], commits, phases)
    assert [r["task"] for r in out[0]["approved"]] == ["a"]


def test_pending_lists_tasks_not_yet_approved():
    phases = [{"initiative": "i", "phase": "p1", "tasks": ["a", "b", "c"], "adds": False}]
    out = stranded_facts([_approved("a")], [_commit("a", "r1", 1, "c1")], phases)
    assert out[0]["pending"] == ["b", "c"]


def test_phase_branch_counts_become_a_phase_branch():
    rows = [{"initiative": "i", "phase": "p1", "branch": "epic/i/p1", "ahead": 2, "behind": 1, "tip": "abc"}]
    assert phase_branch_facts(rows) == [
        {"initiative": "i", "phase": "p1", "branch": "epic/i/p1", "ahead": 2, "behind": 1, "tip": "abc"}
    ]


def test_gather_facts_fills_stranded_and_phase_branches_from_the_seams():
    # `_deps` approves task "a" of phase "p" in initiative "i", run "i-1".
    commit = {**_commit("a", "i-1", 3, "c9"), "phase": "p"}
    deps = replace(
        _deps(),
        run_commits=lambda: [commit],
        phase_state=lambda: [{"initiative": "i", "phase": "p", "tasks": ["a", "b"], "adds": False}],
        branch_counts=lambda: [
            {"initiative": "i", "phase": "p", "branch": "epic/i/p", "ahead": 0, "behind": 4, "tip": "t1"}
        ],
    )
    facts = gather_facts(deps, NOW)
    assert [(s["phase"], s["pending"], [r["run_seq"] for r in s["approved"]]) for s in facts["stranded"]] == [
        ("p", ["b"], [3])
    ]
    assert facts["phase_branches"] == [
        {"initiative": "i", "phase": "p", "branch": "epic/i/p", "ahead": 0, "behind": 4, "tip": "t1"}
    ]


def test_absent_carry_seams_give_empty_facts():
    facts = gather_facts(_deps(), NOW)
    assert (facts["stranded"], facts["phase_branches"]) == ([], [])
