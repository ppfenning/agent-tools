from datetime import UTC, datetime

from agent_tools import stats_schema
from agent_tools.chair_read_tuning import (
    current_lanes,
    current_tiers,
    host_facts,
    last_tuned_at,
    meter_facts,
    read_last_tuned,
    read_stats,
    read_tuning,
    settings_key_for,
    tier_rows,
    tuning_facts,
)
from agent_tools.chair_tune_tiers import TierRow

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _call(task: str, tier: str | None, model: str, cost: float, role: str = "review_charter", challenger: int = 0) -> dict:
    return {
        "run_id": "r1", "task_id": task, "role": role, "tier": tier, "model": model, "model_id": model, "cost_usd": cost,
        "turns": 3, "output_tokens": 10, "attempt": 1, "challenger": challenger, "task_outcome": "landed",
    }


def _task(task: str, verdict: str = "approve", attempts: int = 1) -> dict:
    return {"run_id": "r1", "task_id": task, "outcome": "landed", "charter_verdict": verdict, "fix_loop_attempts": attempts, "fix_loop_stopped": 0}


def test_models_tiers_and_gates_merge_into_one_row_per_role_and_tier_with_per_model_figures():
    calls = [
        _call("a", "cheap", "haiku", 0.5), _call("b", "cheap", "haiku", 0.5), _call("a", "cheap", "haiku", 5.0, challenger=1),
        _call("c", "standard", "sonnet", 1.0), _call("d", "standard", "sonnet", 1.0),
    ]
    tasks = [_task("a"), _task("b", "revise", 2), _task("c"), _task("d")]
    rows = tier_rows(calls, tasks, calls, {"review_charter": "standard"})
    assert rows == [
        {"role": "review_charter", "tier": "cheap", "model": "haiku", "runs": 2, "approval_pct": 50.0,
         "cost_per_approved": 0.5, "settings_key": "policy.review_tier", "current": False},
        {"role": "review_charter", "tier": "standard", "model": "sonnet", "runs": 2, "approval_pct": 100.0,
         "cost_per_approved": 1.0, "settings_key": "policy.review_tier", "current": True},
    ]
    assert [TierRow(**r).tier for r in rows] == ["cheap", "standard"]


def test_a_model_at_two_tiers_is_a_row_per_tier_and_the_role_has_one_current_row():
    calls = [
        _call("a", "cheap", "haiku", 0.5), _call("b", "cheap", "haiku", 0.5), _call("c", "standard", "haiku", 1.0),
        _call("d", "standard", "mini", 1.0), _call("e", "standard", "mini", 1.0), _call("f", "standard", "mini", 1.0),
    ]
    tasks = [_task(t) for t in "abcdef"]
    rows = tier_rows(calls, tasks, calls, {"review_charter": "standard"})
    assert [(r["tier"], r["model"], r["runs"], r["current"]) for r in rows] == [
        ("cheap", "haiku", 2, False), ("standard", "mini", 3, True),
    ]


def test_a_role_with_no_settings_key_or_a_call_with_no_tier_yields_no_row():
    build = [_call("a", "deep", "opus", 2.0, role="build")]
    assert settings_key_for("build") is None
    assert tier_rows(build, [], build, {}) == []
    untiered = [_call("a", None, "haiku", 0.5)]
    assert tier_rows(untiered, [_task("a")], untiered, {}) == []


def test_current_tiers_read_the_cartridge_through_the_settings_model():
    assert current_tiers({"policy": {"review_tier": "cheap"}}) == {"review_charter": "cheap", "review_adversary": "cheap"}


def test_meter_and_limits_land_in_the_right_fields():
    dispatch = {"max_in_flight": 3, "live_runs": 1, "hosts": [
        {"name": "box", "live_runs": 0, "capacity": 5}, {"name": "bare", "live_runs": 0},
    ]}
    lanes = current_lanes(dispatch)
    facts = tuning_facts([], None, 0.4, 0.5, lanes, {"": (1, 4), "box": (2, 6), "bare": (1, 3)})
    assert lanes == {"": 3, "box": 5}
    assert facts == {
        "stats": [],
        "meter": {"weekly_fraction_used": 0.4, "week_elapsed_fraction": 0.5},
        "hosts": {
            "": {"lanes": 3, "min_lanes": 1, "max_lanes": 4},
            "box": {"lanes": 5, "min_lanes": 2, "max_lanes": 6},
        },
        "last_tuned_at": None,
    }
    assert meter_facts(0.1, 0.2) == {"weekly_fraction_used": 0.1, "week_elapsed_fraction": 0.2}
    assert host_facts({}, {"x": (1, 2)}) == {}


def test_the_newest_tuning_action_sets_last_tuned_at_whatever_its_status():
    rows = [
        {"kind": "tune_lanes", "ts": "2026-10-01T00:00:00Z", "status": "ok"},
        {"kind": "propose_tiers", "ts": "2026-10-04T00:00:00Z", "status": "refused"},
        {"kind": "housekeeping", "ts": "2026-10-05T00:00:00Z", "status": "ok"},
    ]
    assert last_tuned_at(rows) == "2026-10-04T00:00:00Z"
    assert last_tuned_at([]) is None


def test_read_stats_reads_seven_days_of_a_real_stats_db(tmp_path):
    db = tmp_path / "stats.db"
    conn = stats_schema.connect(db)
    conn.executemany("INSERT INTO runs (run_id, started_at) VALUES (?, ?)", [("r1", "2026-10-01T00:00:00Z"), ("r0", "2026-09-01T00:00:00Z")])
    conn.executemany(
        "INSERT INTO calls (run_id, role, attempt, tier, model, model_id, cost_usd, turns, output_tokens, task_id, join_confidence, challenger)"
        " VALUES (?, ?, 1, ?, ?, ?, ?, 3, 10, ?, 'exact', ?)",
        [
            ("r1", "review_charter", "cheap", "haiku", "claude-haiku", 0.5, "a", 0),
            ("r1", "review_charter", "cheap", "haiku", "claude-haiku", 0.5, "b", 0),
            ("r1", "review_charter", "cheap", "haiku", "claude-haiku", 5.0, "a", 1),
            ("r1", "review_charter", "standard", "sonnet", "claude-sonnet", 1.0, "c", 0),
            ("r1", "review_charter", "standard", "sonnet", "claude-sonnet", 1.0, "d", 0),
            ("r1", "build", "deep", "opus", "claude-opus", 2.0, "a", 0),
            ("r0", "review_charter", "cheap", "haiku", "claude-haiku", 9.0, "a", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO tasks (run_id, task_id, outcome, charter_verdict, fix_loop_attempts, fix_loop_stopped) VALUES (?, ?, 'landed', ?, ?, 0)",
        [("r1", "a", "approve", 1), ("r1", "b", "revise", 2), ("r1", "c", "approve", 1), ("r1", "d", "approve", 1), ("r0", "a", "approve", 1)],
    )
    conn.commit()
    conn.close()
    rows = read_stats(str(db), {"review_charter": "standard"}, NOW)
    assert rows == [
        {"role": "review_charter", "tier": "cheap", "model": "claude-haiku", "runs": 2, "approval_pct": 50.0,
         "cost_per_approved": 0.5, "settings_key": "policy.review_tier", "current": False},
        {"role": "review_charter", "tier": "standard", "model": "claude-sonnet", "runs": 2, "approval_pct": 100.0,
         "cost_per_approved": 1.0, "settings_key": "policy.review_tier", "current": True},
    ]
    assert read_stats(f"sqlite:///{db}", {"review_charter": "standard"}, NOW) == rows


def test_the_edge_with_no_store_gives_the_empty_entry(tmp_path):
    assert read_tuning(str(tmp_path / "none.db"), tmp_path, {}, NOW) == ([], None)
    assert read_stats("postgresql://h/db", {}, NOW) == []
    assert read_last_tuned(tmp_path) is None
    assert tuning_facts([], None, 0.0, 0.0, {}, {}) == {
        "stats": [], "meter": {"weekly_fraction_used": 0.0, "week_elapsed_fraction": 0.0},
        "hosts": {}, "last_tuned_at": None,
    }
