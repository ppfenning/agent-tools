import json

from test_runs_detail_screen import _write

from agent_tools.dash_detail_run import build


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

    assert result == {
        "timeline": [{"node": "n1", "attempt": 1, "turns": 7, "cost_usd": 0.42, "verdict": "revise"}],
        "verdicts": [{"node": "n1", "attempt": 1, "turns": 7, "cost_usd": 0.42, "verdict": "revise"}],
        "arbiter": "the frobnicator leaks fuel",
        "files": ["a.py", "b.py"],
        "tool_calls": ["Read", "Edit", "Bash"],
        "log_tail": ["reading the parser", "patching parse_row", "short rows now return None"],
    }
