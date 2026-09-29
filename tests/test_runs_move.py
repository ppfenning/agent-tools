from agent_tools.runs_move import move_plan


def test_move_plan_order_and_default_reason():
    assert move_plan("run-7", "widget-initiative", "jarvis") == [
        {"kind": "pause", "run": "run-7", "reason": None},
        {"kind": "stop", "run": "run-7"},
        {"kind": "clear_branches", "initiative": "widget-initiative"},
        {"kind": "relaunch", "initiative": "widget-initiative", "host": "jarvis"},
    ]


def test_move_plan_carries_reason_on_pause_only():
    assert move_plan("run-7", "widget-initiative", "jarvis", reason="rebalance") == [
        {"kind": "pause", "run": "run-7", "reason": "rebalance"},
        {"kind": "stop", "run": "run-7"},
        {"kind": "clear_branches", "initiative": "widget-initiative"},
        {"kind": "relaunch", "initiative": "widget-initiative", "host": "jarvis"},
    ]
