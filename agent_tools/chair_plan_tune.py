"""Pure planner: tune_lanes per changed host and at most one propose_tiers, at most once a day.

Reads `facts["tuning"]` and the tick's clock. Both kinds launch nothing and neither applies a tier or model.

`tuning["stats"]` rows are dicts keyed by the `TierRow` field names. A row missing a required key is skipped.
Per-host spend is not in the rows yet, so lane weights are even; the facts edge owns adding it.
"""

from dataclasses import asdict, fields
from datetime import datetime, timedelta

from agent_tools.chair_tune_lanes import HostLanes, LaneChange, WeeklyFractions, lane_targets
from agent_tools.chair_tune_tiers import TierRow, render_proposals, tier_proposals
from agent_tools.chair_types import Action, Facts

_INTERVAL = timedelta(hours=24)
_ROW_KEYS = tuple(f.name for f in fields(TierRow))
_REQUIRED_ROW_KEYS = tuple(name for name in _ROW_KEYS if name != "current")


def _tuned_within_a_day(last_tuned_at: str | None, now: datetime) -> bool:
    """False for an absent, unparseable or naive timestamp: that counts as never tuned."""
    try:
        parsed = datetime.fromisoformat(last_tuned_at) if last_tuned_at else None
    except ValueError:
        return False
    return parsed is not None and parsed.tzinfo is not None and now - parsed < _INTERVAL


def _tier_row(row: dict[str, object]) -> TierRow | None:
    if any(key not in row for key in _REQUIRED_ROW_KEYS):
        return None
    return TierRow(**{key: row[key] for key in _ROW_KEYS if key in row})  # type: ignore[arg-type]


def _tune_lanes(change: LaneChange, meter: WeeklyFractions) -> Action:
    return {
        "kind": "tune_lanes",
        "host": change.host,
        "from_lanes": change.from_lanes,
        "to_lanes": change.to_lanes,
        "reason": change.reason,
        "evidence": {"used_fraction": meter.used_fraction, "elapsed_fraction": meter.elapsed_fraction},
    }


def plan_tune(facts: Facts, now: datetime) -> list[Action]:
    """Nothing when the tuning entry is empty or the last tune is under 24 hours old."""
    tuning = facts.get("tuning")
    if not tuning or _tuned_within_a_day(tuning.get("last_tuned_at"), now):
        return []
    meter = WeeklyFractions(
        used_fraction=tuning["meter"]["weekly_fraction_used"],
        elapsed_fraction=tuning["meter"]["week_elapsed_fraction"],
    )
    hosts = {name: HostLanes(h["lanes"], h["min_lanes"], h["max_lanes"]) for name, h in tuning["hosts"].items()}
    lanes = [_tune_lanes(change, meter) for change in lane_targets(meter, hosts, {})]
    rows = [r for r in (_tier_row(row) for row in tuning["stats"]) if r is not None]
    proposals = tier_proposals(rows)
    tiers: list[Action] = (
        [{"kind": "propose_tiers", "proposals": [asdict(p) for p in proposals], "body": render_proposals(proposals)}]
        if proposals
        else []
    )
    return [*lanes, *tiers]
