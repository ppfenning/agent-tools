import json

from agent_tools.cli import main
from agent_tools.stats_schema import connect


def _seed(db):
    """t1 failed in r_a and landed on relaunch in r_new; r_old predates --since; two r_new calls cannot be scored."""
    conn = connect(db)
    for run_id, started in (("r_old", "2026-09-01T10:00Z"), ("r_a", "2026-09-15T10:00Z"), ("r_new", "2026-09-20T10:00Z")):
        conn.execute("INSERT INTO runs (run_id, started_at) VALUES (?, ?)", (run_id, started))
    for run_id, task_id, outcome in (
        ("r_old", "t0", "landed"), ("r_a", "t1", "quarantined"), ("r_new", "t1", "landed"), ("r_new", "t2", "landed"),
    ):
        conn.execute("INSERT INTO tasks (run_id, task_id, outcome) VALUES (?, ?, ?)", (run_id, task_id, outcome))
    for run_id, task_id, role, model, cost, turns, attempt in (
        ("r_old", "t0", "build", "big", 9.0, 30, 1),
        ("r_a", "t1", "build", "big", 1.0, 5, 1),
        ("r_new", "t1", "build", "big", 1.5, 10, 2),
        ("r_new", "t2", "build", "small", 0.5, 8, 1),
        ("r_new", "t1", "review_charter", "big", 0.25, 2, 1),
        ("r_new", "t2", "build", "small", None, 4, 1),
        ("r_new", "t9", "build", "small", 0.125, 3, 1),
    ):
        conn.execute(
            "INSERT INTO calls (run_id, seq, role, model, cost_usd, turns, attempt, task_id, join_confidence)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'exact')",
            (run_id, role, model, cost, turns, attempt, task_id),
        )
    conn.commit()
    conn.close()


def _tiers(tmp_path, *flags):
    return main(["stats", "tiers", "--db", str(tmp_path / "s.db"), "--min-samples", "1", *flags])


TEXT = """build
  big: calls 2, tasks 2, $1.2500/call, turns 7.5, landed 50%, first-try 0%, $2.5000/landed
  small: calls 1, tasks 1, $0.5000/call, turns 8.0, landed 100%, first-try 100%, $0.5000/landed
  pick: small (best landed rate: small)
  savings: +1.5000 USD
review_charter
  big: calls 1, tasks 1, $0.2500/call, turns 2.0, landed 100%, first-try -, $0.2500/landed
  not enough evidence (big: 1 tasks)
  savings: +0.0000 USD
skipped 2 calls: no joined task, or no role, model, cost or turns
"""


def test_text_keeps_each_runs_outcome_and_excludes_a_row_before_since(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    assert _tiers(tmp_path, "--since", "2026-09-10") == 0
    assert capsys.readouterr().out == TEXT


def _summary(role, model, calls, tasks, cost, turns, landed, first_try, per_landed):
    return {
        "role": role, "model": model, "calls": calls, "tasks": tasks, "cost_per_call": cost, "avg_turns": turns,
        "landed_rate": landed, "first_try_rate": first_try, "cost_per_landed": per_landed,
    }


def test_json_is_the_core_dict_plus_the_skipped_count(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    assert _tiers(tmp_path, "--since", "2026-09-10", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "summaries": [
            _summary("build", "big", 2, 2, 1.25, 7.5, 0.5, 0.0, 2.5),
            _summary("build", "small", 1, 1, 0.5, 8.0, 1.0, 1.0, 0.5),
            _summary("review_charter", "big", 1, 1, 0.25, 2.0, 1.0, None, 0.25),
        ],
        "recommendations": [
            {
                "role": "build", "pick": "small", "best": "small", "pick_cost_per_call": 0.5, "enough_evidence": True,
                "task_counts": [{"model": "big", "tasks": 2}, {"model": "small", "tasks": 1}],
            },
            {
                "role": "review_charter", "pick": None, "best": None, "pick_cost_per_call": None,
                "enough_evidence": False, "task_counts": [{"model": "big", "tasks": 1}],
            },
        ],
        "savings": {"build": 1.5, "review_charter": 0.0},
        "skipped_calls": 2,
    }


def test_without_since_the_old_row_counts_and_the_db_is_not_written(tmp_path, capsys):
    _seed(tmp_path / "s.db")
    before = (tmp_path / "s.db").read_bytes()
    assert _tiers(tmp_path) == 0
    assert "big: calls 3, tasks 3," in capsys.readouterr().out
    assert (tmp_path / "s.db").read_bytes() == before


def test_pick_names_the_cheaper_model_id_not_the_shared_alias(tmp_path, capsys):
    conn = connect(tmp_path / "s.db")
    conn.execute("INSERT INTO runs (run_id, started_at) VALUES ('r', '2026-09-20T10:00Z')")
    for model_id, cost in (("m-cheap-1", 0.1), ("m-dear-1", 1.0)):
        for n in range(20):
            task_id = f"{model_id}-t{n}"
            conn.execute("INSERT INTO tasks (run_id, task_id, outcome) VALUES ('r', ?, 'landed')", (task_id,))
            conn.execute(
                "INSERT INTO calls (run_id, seq, role, model, model_id, cost_usd, turns, attempt, task_id, join_confidence)"
                " VALUES ('r', ?, 'build', 'sonnet', ?, ?, 5, 1, ?, 'exact')",
                (n, model_id, cost, task_id),
            )
    conn.commit()
    conn.close()
    assert main(["stats", "tiers", "--db", str(tmp_path / "s.db"), "--min-samples", "20"]) == 0
    out = capsys.readouterr().out
    assert "pick: m-cheap-1 (best landed rate: m-cheap-1)" in out
    assert "sonnet" not in out
