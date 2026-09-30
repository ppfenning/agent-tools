import json
import re
from pathlib import Path

from test_runs_detail_screen import _write

from agent_tools.dash_detail_run import build, build_run_detail

_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_BASE_RAW = {
    "run": "dash-feed-1",
    "machine": "omarchy",
    "initiative": "dash-feed-streams-a-versioned-json-snapshot-of",
    "phase": "p1-foundations",
    "alive": True,
    "task": "t1",
    "timeline": [],
    "verdicts": [],
    "arbiter": "",
    "files": [],
    "tool_calls": [],
    "log_tail": [],
    "log_lines": [],
    "fix_loop_stopped": None,
}


def test_build_run_detail_folds_two_raw_nodes_into_one_step():
    raw = {
        **_BASE_RAW,
        "timeline": [
            {"node": "scope_epic", "attempt": 1, "turns": 3, "cost_usd": 0.05, "verdict": ""},
            {"node": "plan", "attempt": 1, "turns": 1, "cost_usd": 0.07, "verdict": "approve"},
            {"node": "build", "attempt": 1, "turns": 2, "cost_usd": 0.02, "verdict": ""},
        ],
    }

    result = build_run_detail(raw)

    plan_step = next(step for step in result["steps"] if step["node"] == "plan")
    assert plan_step == {"node": "plan", "turns": 4, "cost": 0.12, "verdict": "approve", "status": "done"}


def test_build_run_detail_emits_a_step_with_no_folded_entries_as_zeroed_and_pending():
    result = build_run_detail(_BASE_RAW)

    arbitrate_step = next(step for step in result["steps"] if step["node"] == "arbitrate")
    assert arbitrate_step == {"node": "arbitrate", "turns": 0, "cost": 0.0, "verdict": None, "status": "pending"}


def test_build_run_detail_maps_a_tool_call_with_a_timestamp():
    raw = {**_BASE_RAW, "tool_calls": [{"tool": "bash", "summary": "cargo test", "at": "2026-09-29T11:58:00Z"}]}

    result = build_run_detail(raw)

    assert result["last_tool_calls"] == [{"tool": "bash", "summary": "cargo test", "at": "2026-09-29T11:58:00Z"}]


def test_build_run_detail_maps_a_tool_call_without_a_timestamp():
    raw = {**_BASE_RAW, "tool_calls": ["edit: src/feed.rs", "cargo test"]}

    result = build_run_detail(raw)

    assert result["last_tool_calls"] == [
        {"tool": "edit", "summary": "src/feed.rs", "at": ""},
        {"tool": "", "summary": "cargo test", "at": ""},
    ]


def test_build_run_detail_marks_the_current_step_running_for_a_live_run():
    raw = {
        **_BASE_RAW,
        "alive": True,
        "timeline": [
            {"node": "plan", "attempt": 1, "turns": 4, "cost_usd": 0.12, "verdict": "approve"},
            {"node": "build", "attempt": 1, "turns": 12, "cost_usd": 0.84, "verdict": ""},
        ],
    }

    result = build_run_detail(raw)

    assert [step["status"] for step in result["steps"]] == ["done", "running", "pending", "pending", "pending", "pending"]


def test_build_run_detail_leaves_no_running_step_for_a_finished_run():
    raw = {
        **_BASE_RAW,
        "alive": False,
        "timeline": [
            {"node": "plan", "attempt": 1, "turns": 4, "cost_usd": 0.12, "verdict": "approve"},
            {"node": "build", "attempt": 1, "turns": 12, "cost_usd": 0.84, "verdict": ""},
        ],
    }

    result = build_run_detail(raw)

    assert [step["status"] for step in result["steps"]] == ["done", "pending", "pending", "pending", "pending", "pending"]


def test_build_run_detail_reads_stopped_reason_from_a_quarantine_log_line():
    raw = {**_BASE_RAW, "log_lines": ["  quarantined task: t1 — budget exceeded"]}

    result = build_run_detail(raw)

    assert result["stopped_reason"] == "budget exceeded"


def test_build_run_detail_takes_the_quarantine_reason_of_the_current_task_only():
    raw = {
        **_BASE_RAW,
        "task": "t1",
        "log_lines": ["  quarantined task: t1 — timeout", "  quarantined task: t2 — budget exceeded"],
    }

    result = build_run_detail(raw)

    assert result["stopped_reason"] == "timeout"


def test_build_run_detail_ignores_another_tasks_quarantine_line_and_falls_back_to_fix_loop_stopped():
    raw = {
        **_BASE_RAW,
        "task": "t1",
        "log_lines": ["  quarantined task: t2 — budget exceeded"],
        "fix_loop_stopped": "checkpoint",
    }

    result = build_run_detail(raw)

    assert result["stopped_reason"] == "checkpoint"


def test_build_run_detail_falls_back_to_fix_loop_stopped_with_no_log_line():
    raw = {**_BASE_RAW, "fix_loop_stopped": "budget"}

    result = build_run_detail(raw)

    assert result["stopped_reason"] == "budget"


def test_build_run_detail_reads_stopped_reason_as_null_for_a_clean_run():
    result = build_run_detail(_BASE_RAW)

    assert result["stopped_reason"] is None


def test_build_run_detail_matches_the_v1_fixture_keys_and_types_and_round_trips_through_json():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "dash_detail_run_v1.json").read_text())
    raw = {
        **_BASE_RAW,
        "timeline": [
            {"node": "plan", "attempt": 1, "turns": 4, "cost_usd": 0.12, "verdict": "approve"},
            {"node": "build", "attempt": 1, "turns": 12, "cost_usd": 0.84, "verdict": ""},
        ],
        "files": ["src/feed.rs", "tests/fixtures/dash_feed_v1.json"],
        "tool_calls": [
            {"tool": "bash", "summary": "cargo test", "at": "2026-09-29T11:58:00Z"},
            {"tool": "edit", "summary": "src/feed.rs", "at": "2026-09-29T11:55:00Z"},
        ],
        "log_tail": ["running 4 tests", "test feed::tests::parses_fixture ... ok", "test result: ok. 4 passed"],
    }

    result = build_run_detail(raw)

    assert result.keys() == fixture.keys()
    for step_result, step_fixture in zip(result["steps"], fixture["steps"], strict=True):
        assert step_result.keys() == step_fixture.keys()
    for call_result, call_fixture in zip(result["last_tool_calls"], fixture["last_tool_calls"], strict=True):
        assert call_result.keys() == call_fixture.keys()
    assert _AT_PATTERN.match(result["at"])
    assert {**result, "at": fixture["at"]} == fixture
    assert json.loads(json.dumps(result)) == result


def test_build_reshapes_the_runs_top_drill_down_into_a_plain_dict(tmp_path):
    _write(tmp_path / "r1.log", "n1 verdict: revise\n")
    tasks = tmp_path / "r1" / "tasks" / "build"
    tasks.mkdir(parents=True)
    _write(
        tasks / "t1.json",
        json.dumps(
            {
                "change_facts": {"files_touched": ["a.py", "b.py"], "changed_lines": 5},
                "arbitration": {"reasoning": "the frobnicator leaks fuel."},
            }
        ),
    )
    trace = tmp_path / "r1-trace"
    trace.mkdir()
    _write(
        trace / "n1-1.jsonl",
        "\n".join(
            json.dumps(line)
            for line in [
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "reading the parser"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "patching parse_row\nshort rows now return None"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
                {"type": "result", "total_cost_usd": 0.42, "num_turns": 7},
            ]
        )
        + "\n",
    )

    result = build("r1", tmp_path, "2026-09-28T00:00:00Z")

    assert _AT_PATTERN.match(result["at"])
    assert {**result, "at": "IGNORED"} == {
        "schema": 1,
        "kind": "run",
        "at": "IGNORED",
        "run": "r1",
        "machine": None,
        "initiative": None,
        "phase": "build",
        "steps": [
            {"node": step, "turns": 0, "cost": 0.0, "verdict": None, "status": "pending"}
            for step in ("plan", "build", "handoff", "review", "arbitrate", "land")
        ],
        "stopped_reason": None,
        "files": ["a.py", "b.py"],
        "last_tool_calls": [
            {"tool": "", "summary": "Read", "at": ""},
            {"tool": "", "summary": "Edit", "at": ""},
            {"tool": "", "summary": "Bash", "at": ""},
        ],
        "log_tail": ["reading the parser", "patching parse_row", "short rows now return None"],
    }
