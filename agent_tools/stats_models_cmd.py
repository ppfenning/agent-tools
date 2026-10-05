"""Edge for `cox stats models`: read stats.db, hand plain rows to the models core, print the result."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import stats_models, stats_query
from agent_tools.store_dialect import connect_readonly_url


def _scorable(r: Mapping[str, Any]) -> bool:
    """A call with no joined task is skipped, as `stats tiers` and `stats roles` skip it, never counted as not landed."""
    return (
        r["task_outcome"] is not None
        and (r["model_id"] or r["model"]) is not None
        and None not in (r["role"], r["cost_usd"], r["output_tokens"], r["turns"])
    )


def to_model_calls(joined: Sequence[Mapping[str, Any]]) -> list[stats_models.ModelCall]:
    """A task is one run's task; a missing attempt reads as 1."""
    return [
        stats_models.ModelCall(
            role=r["role"],
            model_id=r["model_id"] or r["model"],
            task_id=f"{r['run_id']}/{r['task_id']}",
            cost_usd=r["cost_usd"],
            output_tokens=r["output_tokens"],
            turns=r["turns"],
            attempt=r["attempt"] or 1,
            task_landed=r["task_outcome"] == "landed",
            challenger=bool(r["challenger"]),
        )
        for r in joined
        if _scorable(r)
    ]


def skipped_line(skipped: int) -> str:
    return f"skipped {skipped} calls: no joined task, or no role, model, cost, output tokens or turns"


def run(db: str, role: str | None, since: str | None, as_json: bool) -> int:
    """Read-only: opens the db `mode=ro` and prints; it never writes the db or a profile."""
    if not Path(db).exists():
        print(f"no stats db at {db}: run cox stats ingest", file=sys.stderr)
        return 1
    conn = connect_readonly_url(db)
    try:
        joined = stats_query.tier_call_rows(conn, since)
    finally:
        conn.close()
    in_role = [r for r in joined if role is None or r["role"] == role]
    calls = to_model_calls(in_role)
    skipped = len(in_role) - len(calls)
    summary = stats_models.summarise(calls)
    if as_json:
        print(json.dumps({"models": stats_models.to_json(summary), "skipped_calls": skipped}, indent=2))
    else:
        print("\n".join([*stats_models.render_lines(summary), skipped_line(skipped)]))
    return 0
