"""Pure rule for the per-host lane count that keeps the weekly meter on pace.

Inputs are plain data, so the later reader edge can fill them from the stats
store and the cartridge:

- `meter`: `WeeklyFractions(used_fraction, elapsed_fraction)`. Both are
  fractions in 0..1, not the 0..100 percentages of `usage_meter.Meter`.
- `hosts`: `{host: HostLanes(lanes, min_lanes, max_lanes)}`. `lanes` is the
  host's current lane count; min and max come from the cartridge.
- `spend_by_host`: `{host: usd}` over the last 7 days. A host missing from the
  mapping counts as zero spend.
- `policy`: the resolved pacing `Policy`, optional only so the three-argument
  call works. The edge MUST pass the policy the pacer resolved; the default is
  `usage_window.DEFAULT_POLICY`, which ignores a cartridge's own thresholds.

On pace is the pacer's definition: the ratio `used / elapsed` as in
`pacing._ratio`, judged once `min_elapsed_fraction` of the week has passed.
Over pace is a ratio above `pace_thresholds[0]`. No pace is judged, and no
change returned, when elapsed is zero or below the floor, or when the policy
has no usable first threshold.

Decision for a human: the pacer has no under-pace notion, since everything
at or below its first threshold is on pace. The ticket wants lanes raised
when under pace, so this module mirrors the over-pace edge: under pace is a
ratio below `1 / pace_thresholds[0]`. The pacer is unchanged and may still
call such a ratio on pace.

Caller contract: each call moves a host by at most one lane. The edge must
call this at most once a day, so one noisy day cannot swing the fleet.
"""
from collections.abc import Mapping
from dataclasses import dataclass

from agent_tools.pacing import Policy
from agent_tools.usage_window import DEFAULT_POLICY

__all__ = ["HostLanes", "LaneChange", "WeeklyFractions", "lane_targets"]


@dataclass(frozen=True)
class WeeklyFractions:
    used_fraction: float
    elapsed_fraction: float


@dataclass(frozen=True)
class HostLanes:
    lanes: int
    min_lanes: int
    max_lanes: int


@dataclass(frozen=True)
class LaneChange:
    host: str
    from_lanes: int
    to_lanes: int
    reason: str


def _weights(hosts: Mapping[str, HostLanes], spend_by_host: Mapping[str, float]) -> dict[str, float]:
    """Spend share times host count: an even fleet weighs 1.0 per host."""
    total = sum(spend_by_host.get(name, 0.0) for name in hosts)
    return {
        name: spend_by_host.get(name, 0.0) / total * len(hosts) if total > 0 else 1.0
        for name in hosts
    }


def _desired(host: HostLanes, ratio: float, weight: float) -> int:
    scale = 1.0 + (1.0 / ratio - 1.0) * weight
    return min(host.max_lanes, max(host.min_lanes, round(host.lanes * max(scale, 0.0))))


def _step(current: int, desired: int) -> int:
    """Move at most one lane toward `desired`."""
    return current + (desired > current) - (desired < current)


def _reason(meter: WeeklyFractions, ratio: float, direction: str) -> str:
    return (
        f"used {meter.used_fraction:.0%} of the weekly meter at "
        f"{meter.elapsed_fraction:.0%} of the week elapsed, projecting "
        f"{ratio:.0%} by week end, so {direction} lanes"
    )


def lane_targets(
    meter: WeeklyFractions,
    hosts: Mapping[str, HostLanes],
    spend_by_host: Mapping[str, float],
    policy: Policy = DEFAULT_POLICY,
) -> list[LaneChange]:
    """Per-host lane changes, one lane at most each; empty when on pace or unjudged."""
    if meter.elapsed_fraction <= 0 or meter.elapsed_fraction < policy.min_elapsed_fraction:
        return []
    if not policy.pace_thresholds or policy.pace_thresholds[0] <= 0:
        return []
    over = policy.pace_thresholds[0]
    ratio = max(meter.used_fraction / meter.elapsed_fraction, 1e-9)
    if 1.0 / over <= ratio <= over:
        return []
    direction = "lowering" if ratio > over else "raising"
    weights = _weights(hosts, spend_by_host)
    changes = (
        LaneChange(
            host=name,
            from_lanes=host.lanes,
            to_lanes=_step(host.lanes, _desired(host, ratio, weights[name])),
            reason=_reason(meter, ratio, direction),
        )
        for name, host in sorted(hosts.items())
    )
    return [c for c in changes if c.to_lanes != c.from_lanes]
