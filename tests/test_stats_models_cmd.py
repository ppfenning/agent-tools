import json

from agent_tools import stats_models
from agent_tools.cli import main
from agent_tools.stats_models import ModelCall
from agent_tools.stats_models_cmd import to_model_calls
from agent_tools.stats_schema import connect


def _seed(db):
    """big-1 has a model_id; small has none, so its alias is the id. The last three build calls cannot be scored."""
    conn = connect(db)
    conn.execute("INSERT INTO runs (run_id, started_at) VALUES ('r1', '2026-09-15T10:00Z')")
    for task_id, outcome in (("t1", "landed"), ("t2", "quarantined")):
        conn.execute("INSERT INTO tasks (run_id, task_id, outcome) VALUES ('r1', ?, ?)", (task_id, outcome))
    for seq, (role, model_id, model, task_id, cost, out, turns, attempt, challenger) in enumerate(
        (
            ("build", "big-1", "big", "t1", 1.0, 100, 4, 1, 0),
            ("build", "big-1", "big", "t2", 3.0, 300, 6, 1, 0),
            ("build", None, "small", "t1", 0.5, 50, 2, 2, 0),
            ("build", None, "small", "t1", 0.25, 40, 1, 2, 1),
            ("review_charter", "big-1", "big", "t1", 0.125, 10, 1, 1, 0),
            ("build", "big-1", "big", "t9", 7.0, 700, 9, 1, 0),
            ("build", "big-1", "big", None, 7.0, 700, 9, 1, 0),
            ("build", None, "small", "t1", None, 50, 2, 1, 0),
        ),
        start=1,
    ):
        conn.execute(
            "INSERT INTO calls (run_id, seq, role, model_id, model, task_id, cost_usd, output_tokens, turns,"
            " attempt, challenger, join_confidence) VALUES ('r1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'exact')",
            (seq, role, model_id, model, task_id, cost, out, turns, attempt, challenger),
        )
    conn.commit()
    conn.close()


def _models(tmp_path, *flags):
    return main(["stats", "models", "--db", str(tmp_path / "s.db"), *flags])


BUILD = (
    "role build\n"
    "  big-1: calls 2, challenger calls 0, median cost too few (n=2), p90 cost too few (n=2),"
    " output tokens too few (n=2), turns too few (n=2), cost per landed too few (n=1),"
    " first-try too few (n=2), challenger cost too few (n=0)\n"
    "  small: calls 1, challenger calls 1, median cost too few (n=1), p90 cost too few (n=1),"
    " output tokens too few (n=1), turns too few (n=1), cost per landed too few (n=1),"
    " first-try too few (n=1), challenger cost too few (n=1)\n"
)
SKIPPED_3 = "skipped 3 calls: no joined task, or no role, model, cost, output tokens or turns\n"

TEXT = BUILD + (
    "role review_charter\n"
    "  big-1: calls 1, challenger calls 0, median cost too few (n=1), p90 cost too few (n=1),"
    " output tokens too few (n=1), turns too few (n=1), cost per landed too few (n=1),"
    " challenger cost too few (n=0)\n"
) + SKIPPED_3


def test_text_shows_both_model_ids_with_too_few_cells_and_skips_unjoined_calls(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    before = (tmp_path / "s.db").read_bytes()
    assert _models(tmp_path) == 0
    assert capsys.readouterr().out == TEXT
    assert (tmp_path / "s.db").read_bytes() == before


def test_role_keeps_one_block(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    assert _models(tmp_path, "--role", "build") == 0
    assert capsys.readouterr().out == BUILD + SKIPPED_3


def test_since_is_inclusive_of_the_run_start_date(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    assert _models(tmp_path, "--since", "2026-09-15") == 0
    assert capsys.readouterr().out == TEXT
    assert _models(tmp_path, "--since", "2026-09-16") == 0
    assert capsys.readouterr().out == "skipped 0 calls: no joined task, or no role, model, cost, output tokens or turns\n"


def test_json_is_the_core_dict_plus_the_skipped_count(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    assert _models(tmp_path, "--role", "review_charter", "--json") == 0
    too_few = {"value": "too few", "count": 1}
    assert json.loads(capsys.readouterr().out) == {
        "models": {
            "review_charter": {
                "big-1": {
                    "calls": {"value": 1, "count": 1}, "median_cost": too_few, "p90_cost": too_few,
                    "output_tokens": too_few, "turns": too_few, "cost_per_landed": too_few, "first_try": None,
                    "challenger_calls": {"value": 0, "count": 0}, "challenger_cost": {"value": "too few", "count": 0},
                }
            }
        },
        "skipped_calls": 0,
    }


def test_bad_since_is_a_usage_error(tmp_path):
    assert _models(tmp_path, "--since", "20260901") == 2


def _row(role, model_id, model, task_id, cost, out, turns, attempt, outcome, challenger=0):
    return {
        "role": role, "model_id": model_id, "model": model, "run_id": "r1", "task_id": task_id, "cost_usd": cost,
        "output_tokens": out, "turns": turns, "attempt": attempt, "task_outcome": outcome, "challenger": challenger,
    }


ROWS = [
    _row("build", "big-1", "big", "t1", 1.0, 100, 4, 1, "landed"),
    _row("build", "big-1", "big", "t2", 3.0, 300, 6, 1, "quarantined"),
    _row("build", None, "small", "t1", 0.5, 50, 2, None, "landed"),
    _row("build", "big-1", "big", "t9", 7.0, 700, 9, 1, None),
    _row("review_charter", "small-2", "small", "t1", 0.25, 10, 1, 1, "landed"),
]


def test_adapter_prefers_model_id_defaults_attempt_and_drops_unjoined_rows():
    assert to_model_calls(ROWS) == [
        ModelCall("build", "big-1", "r1/t1", 1.0, 100, 4, 1, True, False),
        ModelCall("build", "big-1", "r1/t2", 3.0, 300, 6, 1, False, False),
        ModelCall("build", "small", "r1/t1", 0.5, 50, 2, 1, True, False),
        ModelCall("review_charter", "small-2", "r1/t1", 0.25, 10, 1, 1, True, False),
    ]


def test_adapted_rows_give_the_figures_once_the_threshold_is_met():
    lines = stats_models.render_lines(stats_models.summarise(to_model_calls(ROWS)), min_count=1)
    assert lines == [
        "role build",
        "  big-1: calls 2, challenger calls 0, median cost 2.00 (n=2), p90 cost 3.00 (n=2), output tokens 200 (n=2),"
        " turns 5.0 (n=2), cost per landed 1.00 (n=1), first-try 50% (n=2), challenger cost too few (n=0)",
        "  small: calls 1, challenger calls 0, median cost 0.50 (n=1), p90 cost 0.50 (n=1), output tokens 50 (n=1),"
        " turns 2.0 (n=1), cost per landed 0.50 (n=1), first-try 100% (n=1), challenger cost too few (n=0)",
        "role review_charter",
        "  small-2: calls 1, challenger calls 0, median cost 0.25 (n=1), p90 cost 0.25 (n=1), output tokens 10 (n=1),"
        " turns 1.0 (n=1), cost per landed 0.25 (n=1), challenger cost too few (n=0)",
    ]
