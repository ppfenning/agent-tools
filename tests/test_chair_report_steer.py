from datetime import UTC, datetime

from agent_tools.chair_report import format_status

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)
FACTS = {
    "limits": {"hard_stop": False, "weekly_fraction": 0.61, "hard_stop_fraction": 0.9, "launch_cap": 2,
               "go_degraded": False, "five_hour_fraction": 0.42, "window_start_day": None,
               "window_source": "meter", "weekly_source": "meter"},
    "dispatch": {"max_in_flight": 4, "live_runs": 2},
}


def _result(kind, status, **fields):
    return {"action": {"kind": kind, **fields}, "status": status, "reason": ""}


def _steer(initiative, other):
    return _result("steer_clear", "recorded", initiative=initiative, other=other, paths=["src/main.rs"])


def test_two_steer_clear_results_show_their_pairs():
    line = format_status(FACTS, [], [_steer("x", "y"), _steer("z", "w")], NOW)
    assert " | steered: x~y, z~w | needs chair: none" in line
    assert "src/main.rs" not in line


def test_no_steer_clear_leaves_the_line_unchanged():
    assert "steered" not in format_status(FACTS, [], [], NOW)


def test_steered_follows_launched_and_failed():
    results = [
        _result("launch_epic", "done", initiative="a"), _result("pull", "refused", initiative="c"), _steer("x", "y"),
    ]
    line = format_status(FACTS, [], results, NOW)
    assert line.index("launched:") < line.index("failed:") < line.index("steered:")
