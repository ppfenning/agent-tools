from __future__ import annotations

import copy

from agent_tools.dash_cost_series import run_cost_series


def _call(at: str, cost: float, node: str) -> dict:
    return {"at": at, "cost_usd": cost, "node": node}


def test_three_calls_yield_three_cumulative_points() -> None:
    calls = [
        _call("t1", 0.5, "plan"),
        _call("t2", 0.25, "build"),
        _call("t3", 1.0, "review"),
    ]
    assert run_cost_series(calls) == [
        ["t1", 0.5, "plan"],
        ["t2", 0.75, "build"],
        ["t3", 1.75, "review"],
    ]


def test_seventy_calls_keep_last_sixty_with_full_prefix() -> None:
    calls = [_call(f"t{i}", 1.0, "build") for i in range(70)]
    series = run_cost_series(calls)
    assert len(series) == 60
    assert series[0] == ["t10", 11.0, "build"]
    assert series[-1] == ["t69", 70.0, "build"]


def test_no_calls_yields_empty_list() -> None:
    assert run_cost_series([]) == []


def test_input_records_unchanged() -> None:
    calls = [_call("t1", 0.5, "plan"), _call("t2", 0.25, "build")]
    before = copy.deepcopy(calls)
    run_cost_series(calls)
    assert calls == before
