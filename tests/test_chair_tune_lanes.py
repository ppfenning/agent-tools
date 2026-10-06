from dataclasses import replace

from agent_tools.chair_tune_lanes import HostLanes, LaneChange, WeeklyFractions, lane_targets
from agent_tools.usage_window import DEFAULT_POLICY

OVER = WeeklyFractions(0.8, 0.5)  # ratio 1.6
UNDER = WeeklyFractions(0.4, 0.5)  # ratio 0.8


def test_over_pace_lowers_a_busy_host_by_one():
    hosts = {"busy": HostLanes(4, 1, 8), "idle": HostLanes(4, 1, 8)}
    assert lane_targets(OVER, hosts, {"busy": 10.0, "idle": 0.0}) == [
        LaneChange(
            "busy",
            4,
            3,
            "used 80% of the weekly meter at 50% of the week elapsed, "
            "projecting 160% by week end, so lowering lanes",
        )
    ]


def test_under_pace_raises_a_host_by_one():
    # 4 lanes * (1 / 0.8) = 5: one lane away, so the step cap does not decide.
    hosts = {"busy": HostLanes(4, 1, 8)}
    assert lane_targets(UNDER, hosts, {"busy": 5.0}) == [
        LaneChange(
            "busy",
            4,
            5,
            "used 40% of the weekly meter at 50% of the week elapsed, "
            "projecting 80% by week end, so raising lanes",
        )
    ]


def test_even_spend_scales_each_host_by_inverse_pace():
    # Weight 1.0 each: 2 * 0.625 = 1.25 -> 1. A share-only weight of 0.5 would keep 2.
    hosts = {"a": HostLanes(2, 1, 8), "b": HostLanes(2, 1, 8)}
    changes = lane_targets(OVER, hosts, {"a": 5.0, "b": 5.0})
    assert [(c.host, c.to_lanes) for c in changes] == [("a", 1), ("b", 1)]


def test_larger_spend_share_takes_more_of_the_cut():
    # Weights 1.5 and 0.5: a -> 0.875 -> 1, b -> 1.625 -> 2. Even scaling would lower both.
    hosts = {"a": HostLanes(2, 1, 8), "b": HostLanes(2, 1, 8)}
    changes = lane_targets(OVER, hosts, {"a": 3.0, "b": 1.0})
    assert [(c.host, c.to_lanes) for c in changes] == [("a", 1)]


def test_on_pace_gives_no_change():
    hosts = {"big": HostLanes(40, 1, 80)}
    # Ratios 1.2 and 0.84 sit inside the band; unbanded, 40 lanes would move.
    assert lane_targets(WeeklyFractions(0.6, 0.5), hosts, {"big": 5.0}) == []
    assert lane_targets(WeeklyFractions(0.42, 0.5), hosts, {"big": 5.0}) == []


def test_policy_threshold_decides_over_pace():
    policy = replace(DEFAULT_POLICY, pace_thresholds=(2.0,))
    assert lane_targets(OVER, {"a": HostLanes(4, 1, 8)}, {"a": 5.0}, policy) == []


def test_host_at_max_is_not_raised():
    hosts = {"busy": HostLanes(6, 1, 6)}
    assert lane_targets(UNDER, hosts, {"busy": 5.0}) == []


def test_host_at_min_is_not_lowered():
    # Unclamped: 2 * 0.625 = 1.25 -> 1, below the minimum of 2.
    hosts = {"busy": HostLanes(2, 2, 6)}
    assert lane_targets(OVER, hosts, {"busy": 5.0}) == []


def test_target_two_lanes_away_moves_only_one():
    # 2 lanes * (1 / 0.4) = 5 desired.
    hosts = {"busy": HostLanes(2, 1, 8)}
    (change,) = lane_targets(WeeklyFractions(0.2, 0.5), hosts, {"busy": 5.0})
    assert (change.from_lanes, change.to_lanes) == (2, 3)


def test_elapsed_zero_gives_no_change():
    hosts = {"busy": HostLanes(4, 1, 8)}
    assert lane_targets(WeeklyFractions(0.3, 0.0), hosts, {"busy": 5.0}) == []


def test_elapsed_zero_with_a_zero_floor_policy_gives_no_change():
    policy = replace(DEFAULT_POLICY, min_elapsed_fraction=0.0)
    hosts = {"busy": HostLanes(4, 1, 8)}
    assert lane_targets(WeeklyFractions(0.3, 0.0), hosts, {"busy": 5.0}, policy) == []


def test_policy_without_a_first_threshold_gives_no_change():
    hosts = {"busy": HostLanes(4, 1, 8)}
    for thresholds in ((), (0.0,)):
        policy = replace(DEFAULT_POLICY, pace_thresholds=thresholds)
        assert lane_targets(OVER, hosts, {"busy": 5.0}, policy) == []
