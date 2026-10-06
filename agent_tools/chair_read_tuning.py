"""The `tuning` source of `chair_facts`: 7 days of stats, the weekly meter and the lane limits, as plain data.

Each `stats` row is a `chair_tune_tiers.TierRow` dict, one per role and tier, built from the three `cox stats`
results over the calls that ran at that tier. A tier is a partition, so a model that ran at two tiers is counted
at each, and the role has at most one row per tier and so at most one `current` row.

- model: the model with the most runs at the tier; runs: its distinct tasks (`stats_tiers`).
- approval_pct: the build role's first-try rate (`stats_tiers`); a gate role's `1 - changed_rate` from
  `stats_gates.gate_rows` run over that model's own calls at the tier, so it differs between models.
- cost_per_approved: `stats_models` cost per landed task, else the `stats_tiers` one.
- settings_key: `settings_key_for(role)`; current: the tier is the role's configured tier.

A row is dropped, not guessed, when its role has no registered settings key, or it has no approval or cost figure.
The settings registry has no build-tier key, so the build role yields no row today.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_tools import (
    run_store,
    settings_model,
    stats_gates,
    stats_models,
    stats_models_cmd,
    stats_query,
    stats_tiers,
    stats_tiers_cmd,
)
from agent_tools.chair_types import HostTuning, MeterFacts, TuningFacts
from agent_tools.store_dialect import connect_readonly_url, is_postgres

WINDOW_DAYS = 7
TUNING_KINDS = frozenset({"tune_lanes", "propose_tiers"})
ROLE_KEYS = {
    "review_charter": "policy.review_tier",
    "review_adversary": "policy.review_tier",
    "plan_competition": "policy.plan_competition.min_tier",
}
READ_ERRORS = (*run_store._DB_ERRORS, RuntimeError, OSError)

Row = dict[str, Any]
Rows = Sequence[Mapping[str, Any]]


def settings_key_for(role: str) -> str | None:
    """The `cox settings set` key for a role's tier, only when the settings registry holds it."""
    key = ROLE_KEYS.get(role)
    return key if key is not None and settings_model.setting_for("cartridge", key) is not None else None


def current_tiers(cartridge: dict) -> dict[str, str]:
    """Role to its configured tier, read through the settings model; a role whose key the cartridge lacks is absent."""
    values = {r.key: r.value for rows in settings_model.rows(cartridge, {}, {}).values() for r in rows}
    return {role: values[key] for role, key in ROLE_KEYS.items() if isinstance(values.get(key), str)}


def _model_key(row: Mapping[str, Any]) -> str | None:
    """The model id, else the alias, as `cox stats tiers` keys a call."""
    return row.get("model_id") if row.get("model_id") is not None else row.get("model")


def _approval(summary: stats_tiers.ModelSummary, tasks: Rows, calls: Rows) -> float | None:
    if summary.first_try_rate is not None:
        return summary.first_try_rate * 100
    mine = [c for c in calls if c.get("role") == summary.role and _model_key(c) == summary.model]
    gates = stats_gates.gate_rows(tasks, mine)
    rate = gates[0]["changed_rate"] if gates else None
    return None if rate is None else (1 - rate) * 100


def _cost(summary: stats_tiers.ModelSummary, models: Mapping[str, Mapping[str, stats_models.ModelRow]]) -> float | None:
    cell = models.get(summary.role, {}).get(summary.model)
    landed = cell.cost_per_landed.value if cell is not None else None
    return landed if landed is not None else summary.cost_per_landed


def _tier_partition(tier: str, joined: Rows, tasks: Rows, calls: Rows, current: Mapping[str, str]) -> list[Row]:
    models = stats_models.summarise(stats_models_cmd.to_model_calls(joined))
    candidates = [
        {
            "role": s.role, "tier": tier, "model": s.model, "runs": s.tasks, "approval_pct": approval,
            "cost_per_approved": cost, "settings_key": settings_key_for(s.role), "current": current.get(s.role) == tier,
        }
        for s in stats_tiers.summarise(stats_tiers_cmd.to_tier_rows(joined))
        for approval, cost in [(_approval(s, tasks, calls), _cost(s, models))]
    ]
    usable = sorted(
        (c for c in candidates if None not in (c["settings_key"], c["approval_pct"], c["cost_per_approved"])),
        key=lambda c: (c["role"], -c["runs"], c["model"]),
    )
    return [next(c for c in usable if c["role"] == role) for role in sorted({c["role"] for c in usable})]


def tier_rows(joined: Rows, gate_tasks: Rows, gate_calls: Rows, current: Mapping[str, str]) -> list[Row]:
    """`TierRow`-shaped dicts from `stats_query.tier_call_rows` and `gates_inputs` output, as the module says."""
    tiers = sorted({r["tier"] for r in joined if r.get("tier")})
    return [
        row
        for tier in tiers
        for row in _tier_partition(
            tier, [r for r in joined if r.get("tier") == tier], gate_tasks,
            [c for c in gate_calls if c.get("tier") == tier], current,
        )
    ]


def meter_facts(used_fraction: float, week_elapsed_fraction: float) -> MeterFacts:
    return {"weekly_fraction_used": used_fraction, "week_elapsed_fraction": week_elapsed_fraction}


def current_lanes(dispatch: Mapping[str, Any]) -> dict[str, int]:
    """Host to lane count: the local machine under "" at `max_in_flight`, a lane host only when it carries a capacity."""
    return {"": int(dispatch["max_in_flight"]), **{h["name"]: int(h["capacity"]) for h in dispatch["hosts"] if "capacity" in h}}


def host_facts(lanes: Mapping[str, int], bounds: Mapping[str, tuple[int, int]]) -> dict[str, HostTuning]:
    """A host with both a lane count and a (min, max) bound; any other host is left out."""
    return {
        name: {"lanes": lanes[name], "min_lanes": lo, "max_lanes": hi}
        for name, (lo, hi) in bounds.items() if name in lanes
    }


def last_tuned_at(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """The greatest `ts` among tune_lanes and propose_tiers rows, whatever their status; None with none."""
    found = [str(r["ts"]) for r in rows if r.get("kind") in TUNING_KINDS and r.get("ts")]
    return max(found) if found else None


def tuning_facts(
    stats: Sequence[Row], last: str | None, used_fraction: float, week_elapsed_fraction: float,
    lanes: Mapping[str, int], bounds: Mapping[str, tuple[int, int]],
) -> TuningFacts:
    return {
        "stats": list(stats), "meter": meter_facts(used_fraction, week_elapsed_fraction),
        "hosts": host_facts(lanes, bounds), "last_tuned_at": last,
    }


def since_day(now: datetime) -> str:
    return (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")


def sqlite_path(url: str) -> Path | None:
    """The file a sqlite path or `sqlite:` URL names, as `connect_readonly_url` resolves it; None for postgres."""
    if is_postgres(url):
        return None
    return Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url)


def read_stats(stats_db: str, current: Mapping[str, str], now: datetime) -> list[Row]:
    """Edge. The tier rows over the last 7 days of stats.db; [] when the db is absent, postgres or unreadable."""
    path = sqlite_path(stats_db)
    since = since_day(now)
    try:
        if path is None or not path.exists():
            return []
        conn = connect_readonly_url(stats_db)
        try:
            joined = stats_query.tier_call_rows(conn, since)
            tasks, calls = stats_query.gates_inputs(conn, since)
        finally:
            conn.close()
    except READ_ERRORS:
        return []
    return tier_rows(joined, tasks, calls, current)


def read_last_tuned(runs_dir: Path) -> str | None:
    """Edge. None with no store, no `chair_actions` table or an unreadable store."""
    try:
        opened = run_store._open(runs_dir)
        if opened is None:
            return None
        conn, token = opened
        try:
            sql = run_store._sql("SELECT ts, kind FROM chair_actions WHERE kind IN ({p}, {p})", token)
            rows = [dict(r) for r in conn.execute(sql, tuple(sorted(TUNING_KINDS))).fetchall()]
        finally:
            conn.close()
    except READ_ERRORS:
        return None
    return last_tuned_at(rows)


def read_tuning(stats_db: str, runs_dir: Path, current: Mapping[str, str], now: datetime) -> tuple[list[Row], str | None]:
    """Edge. The `FactsDeps.tuning` source once bound: `functools.partial(read_tuning, stats_db, runs_dir, current)`."""
    return read_stats(stats_db, current, now), read_last_tuned(runs_dir)
