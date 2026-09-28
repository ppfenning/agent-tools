from datetime import UTC, datetime

from agent_tools.chair_exec import Deps, argv_for, perform
from agent_tools.chair_facts import STRANDED_CAUSE, approved_facts, quarantine_facts
from agent_tools.chair_plan_land import plan_lands, planned_tasks
from agent_tools.chair_read_approved import with_runs
from agent_tools.chair_report import format_status

STRANDED = [{"run": "x-1", "task": "t", "phase": "p", "branch": None, "remedy": None}]
TASK = {"id": "t", "initiative": "x", "repo": "/r", "phase": "p", "phase_done": True, "needs": [], "run": "x-1", "needs_fetch": False}


def _causes(approved):
    planned = planned_tasks(approved_facts(approved), [])
    return [q["cause"] for q in quarantine_facts([], STRANDED, [], set(), lambda initiative, task: False, planned)]


def _perform(action, code=0, output="merge: ok\nmark_done: ok"):
    recorded = []
    deps = Deps(
        run=lambda argv: (code, output), delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "", record=recorded.append,
        run_id=lambda a: "unused", repo_for=lambda a: "",
    )
    return perform([action], deps, lambda: 1, False), recorded


def test_land_argv_names_the_run_holding_the_approved_record():
    facts = {"approved": approved_facts([TASK]), "initiatives": []}
    assert argv_for(plan_lands(facts)[0]) == ["cox", "runs", "land", "x-1", "--task", "t", "--repo", "/r", "--apply", "--no-claim"]


def test_land_without_a_run_has_no_argv():
    assert argv_for({"kind": "land", "task_id": "t", "repo": "/r"}) is None


def test_stranded_row_for_a_task_being_landed_needs_no_chair():
    assert _causes([TASK]) == []


def test_stranded_row_for_an_unplanned_task_still_needs_a_chair():
    assert _causes([]) == [STRANDED_CAUSE]


def test_stranded_row_for_a_task_the_schedule_defers_still_needs_a_chair():
    assert _causes([{**TASK, "needs": ["unknown"]}]) == [STRANDED_CAUSE]


def test_stranded_row_for_a_task_with_no_run_still_needs_a_chair():
    assert _causes([{**TASK, "run": ""}]) == [STRANDED_CAUSE]


def test_with_runs_takes_the_newest_run_by_number():
    stranded = [{"run": "x-10", "task": "t", "phase": "p"}, {"run": "x-2", "task": "t", "phase": "p"}]
    assert with_runs([{**TASK, "run": ""}], stranded)[0]["run"] == "x-10"


def test_with_runs_ignores_the_same_task_id_in_another_phase():
    assert with_runs([{**TASK, "run": ""}], [{"run": "x-3", "task": "t", "phase": "other"}])[0]["run"] == ""


def test_a_stranded_row_in_another_phase_is_not_dropped_by_the_land():
    other = [{"run": "x-3", "task": "t", "phase": "other"}]
    planned = planned_tasks(approved_facts([TASK]), [])
    assert [q["cause"] for q in quarantine_facts([], other, [], set(), lambda i, t: False, planned)] == [STRANDED_CAUSE]


def test_a_land_that_refuses_escalates_to_needs_chair_with_its_cause():
    land = {"kind": "land", "task_id": "t", "repo": "/r", "run": "x-1", "initiative": "x", "epoch": 1}
    results, recorded = _perform(land, code=1, output="merge: conflict")
    assert [(r["action"]["kind"], r["status"]) for r in results] == [("land", "refused"), ("needs_chair", "escalated")]
    assert [(a["kind"], a["status"]) for a in recorded] == [("land", "refused"), ("needs_chair", "escalated")]
    assert {k: v for k, v in recorded[1].items() if k not in ("status", "reason")} == {
        "kind": "needs_chair", "initiative": "x", "task_id": "t", "cause": "land", "epoch": 1,
    }


def test_a_refused_land_without_a_run_escalates_to_needs_chair():
    results, _ = _perform({"kind": "land", "task_id": "t", "repo": "/r", "initiative": "x", "epoch": 1})
    assert [r["status"] for r in results] == ["refused", "escalated"]


def test_a_landed_land_raises_no_needs_chair():
    results, recorded = _perform({"kind": "land", "task_id": "t", "repo": "/r", "run": "x-1", "initiative": "x", "epoch": 1})
    assert ([r["status"] for r in results], [(a["kind"], a["status"]) for a in recorded]) == (["landed"], [("land", "landed")])


def test_the_status_line_names_an_escalated_land():
    facts = {
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "five_hour_fraction": None},
        "dispatch": {"max_in_flight": 4, "live_runs": 0},
    }
    results, _ = _perform({"kind": "land", "task_id": "t", "repo": "/r", "initiative": "x", "epoch": 1})
    assert format_status(facts, [], results, datetime(2026, 9, 26, tzinfo=UTC)).endswith("needs chair: x:stranded")
