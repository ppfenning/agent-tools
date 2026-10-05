import json

from agent_tools.stats_models import (
    ModelCall,
    render_lines,
    summarise,
    to_json,
)


def call(
    role="build",
    model="m1",
    task="t1",
    cost=1.0,
    tokens=100,
    turns=2,
    attempt=1,
    landed=True,
    challenger=False,
) -> ModelCall:
    return ModelCall(role, model, task, cost, tokens, turns, attempt, landed, challenger)


def test_median_and_p90():
    rows = [call(task=f"t{i}", cost=float(i)) for i in range(1, 11)]
    row = summarise(rows)["build"]["m1"]
    assert (row.median_cost.value, row.p90_cost.value) == (5.5, 9.0)
    assert (row.median_cost.count, row.p90_cost.count) == (10, 10)


def test_cost_per_landed_with_zero_landed():
    rows = [call(task="a", landed=False), call(task="b", landed=False)]
    figure = summarise(rows)["build"]["m1"].cost_per_landed
    assert (figure.value, figure.count) == (None, 0)


def test_cost_per_landed_counts_distinct_tasks():
    rows = [
        call(task="a", cost=1.0, attempt=1, landed=True),
        call(task="a", cost=3.0, attempt=2, landed=True),
        call(task="b", cost=5.0, landed=False),
    ]
    figure = summarise(rows)["build"]["m1"].cost_per_landed
    assert (figure.value, figure.count) == (4.0, 1)


def test_first_try_set_only_for_build():
    shape = [
        call(task="a", attempt=1, landed=True),
        call(task="b", attempt=2, landed=True),
    ]
    review = [call(role="review", task=r.task_id, attempt=r.attempt) for r in shape]
    out = summarise(shape + review)
    assert (out["build"]["m1"].first_try.value, out["build"]["m1"].first_try.count) == (
        0.5,
        2,
    )
    assert out["review"]["m1"].first_try is None


def test_cell_cut_at_19_and_20_calls():
    nineteen = [call(model="few", task=f"t{i}", cost=2.0) for i in range(19)]
    twenty = [call(model="enough", task=f"t{i}", cost=2.0) for i in range(20)]
    lines = render_lines(summarise(nineteen + twenty))
    few = next(x for x in lines if x.startswith("  few:"))
    enough = next(x for x in lines if x.startswith("  enough:"))
    assert "median cost too few (n=19)" in few
    assert "median cost 2.00 (n=20)" in enough
    cells = to_json(summarise(nineteen + twenty), min_count=20)["build"]
    assert cells["few"]["median_cost"] == {"value": "too few", "count": 19}
    assert cells["enough"]["median_cost"] == {"value": 2.0, "count": 20}
    assert json.loads(json.dumps(cells)) == cells


def test_challenger_rows_stay_out_of_standard_figures():
    rows = [
        call(task="a", cost=1.0),
        call(task="b", cost=1.0),
        call(task="c", cost=10.0, challenger=True),
        call(task="d", cost=20.0, challenger=True),
    ]
    row = summarise(rows)["build"]["m1"]
    assert (row.calls.count, row.median_cost.value) == (2, 1.0)
    assert (row.challenger_calls.count, row.challenger_cost.value) == (2, 15.0)


def test_two_roles_never_mixed():
    rows = [
        call(role="build", model="m1", cost=1.0),
        call(role="review", model="m1", cost=9.0),
    ]
    out = summarise(rows)
    assert sorted(out) == ["build", "review"]
    assert out["build"]["m1"].median_cost.value == 1.0
    assert out["review"]["m1"].median_cost.value == 9.0
    lines = render_lines(out)
    assert lines[0] == "role build"
    assert lines[2] == "role review"
    assert all(not ("build" in x and "review" in x) for x in lines)
