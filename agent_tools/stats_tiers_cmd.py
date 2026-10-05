"""Edge for `cox stats tiers`: read stats.db, hand plain rows to the tiers core, print the result."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import stats_query, stats_tiers
from agent_tools.store_dialect import connect_readonly_url


def _model_key(r: Mapping[str, Any]) -> str | None:
    """The model id when the call carries one, else the alias; a row from a db without the column reads as the alias."""
    model_id = r.get("model_id")
    return r["model"] if model_id is None else model_id


def _scorable(r: Mapping[str, Any]) -> bool:
    return r["task_outcome"] is not None and None not in (r["role"], _model_key(r), r["cost_usd"], r["turns"])


def to_tier_rows(joined: Sequence[Mapping[str, Any]]) -> list[stats_tiers.TierRow]:
    """A task is one run's task, so a relaunch in another run is a second task. A missing attempt reads as 1."""
    return [
        stats_tiers.TierRow(
            role=r["role"],
            model=_model_key(r),
            task_id=f"{r['run_id']}/{r['task_id']}",
            cost_usd=r["cost_usd"],
            turns=r["turns"],
            attempt=r["attempt"] or 1,
            task_landed=r["task_outcome"] == "landed",
        )
        for r in joined
        if _scorable(r)
    ]


def skipped_line(skipped: int) -> str:
    return f"skipped {skipped} calls: no joined task, or no role, model, cost or turns"


def run(db: str, since: str | None, min_samples: int, as_json: bool) -> int:
    """Read-only: opens the db `mode=ro` and prints; it never writes the db or a profile."""
    if not Path(db).exists():
        print(f"no stats db at {db}: run cox stats ingest", file=sys.stderr)
        return 1
    conn = connect_readonly_url(db)
    try:
        joined = stats_query.tier_call_rows(conn, since)
    finally:
        conn.close()
    rows = to_tier_rows(joined)
    skipped = len(joined) - len(rows)
    summaries = stats_tiers.summarise(rows)
    recommendations = stats_tiers.recommend(summaries, min_samples)
    saved = stats_tiers.savings(rows, recommendations)
    if as_json:
        print(json.dumps({**stats_tiers.to_json(summaries, recommendations, saved), "skipped_calls": skipped}, indent=2))
    else:
        print("\n".join([*stats_tiers.render_lines(summaries, recommendations, saved), skipped_line(skipped)]))
    return 0
