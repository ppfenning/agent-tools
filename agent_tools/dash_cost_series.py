"""Pure run cost series: cumulative cost per node call, last 60 points.

A call record is a mapping with keys "at", "cost_usd" and "node".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import accumulate
from typing import Any

LIMIT = 60


def run_cost_series(calls: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    """Return [at, cumulative_cost_usd, node] per call, summed over all calls, then the last LIMIT kept."""
    totals = accumulate(call["cost_usd"] for call in calls)
    points = [
        [call["at"], total, call["node"]] for call, total in zip(calls, totals)
    ]
    return points[-LIMIT:]
