from __future__ import annotations

import dataclasses

import pytest

from agent_tools.forge_status import ForgeStatus, StatusReader, parse_github_summary

OPERATIONAL = {
    "components": [
        {"name": "Actions", "status": "operational"},
        {"name": "CI Runners", "status": "operational"},
    ],
    "incidents": [],
}
ACTIONS_DOWN = {
    "components": [{"name": "Actions", "status": "major_outage"}],
    "incidents": [{"name": "Actions queue delays"}],
}
CI_DOWN = {
    "components": [
        {"name": "Issues", "status": "operational"},
        {"name": "CI Runners", "status": "degraded_performance"},
    ],
    "incidents": [],
}
ISSUES_DOWN = {
    "components": [{"name": "Issues", "status": "major_outage"}],
    "incidents": [{"name": "Issues down"}],
}


class FakeFetch:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0
        self.fail = False

    def __call__(self) -> dict:
        self.calls += 1
        if self.fail:
            raise OSError("unreachable")
        return self.payload


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_operational_payload() -> None:
    assert parse_github_summary(OPERATIONAL, 5.0) == ForgeStatus(False, "", 5.0)


def test_degraded_actions_carries_incident_name() -> None:
    status = parse_github_summary(ACTIONS_DOWN)
    assert (status.degraded, status.incident) == (True, "Actions queue delays")


def test_degraded_ci_component_carries_component_name() -> None:
    status = parse_github_summary(CI_DOWN)
    assert (status.degraded, status.incident) == (True, "CI Runners")


def test_other_component_down_is_not_degraded() -> None:
    assert parse_github_summary(ISSUES_DOWN).degraded is False


def test_status_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ForgeStatus(False, "", 0.0).degraded = True  # type: ignore[misc]


def test_read_inside_window_does_not_refetch() -> None:
    fetch, clock = FakeFetch(OPERATIONAL), FakeClock(1000.0)
    reader = StatusReader(fetch, clock)
    first = reader.read()
    clock.now = 1299.0
    assert reader.read() is first
    assert fetch.calls == 1


def test_read_after_window_refetches() -> None:
    fetch, clock = FakeFetch(OPERATIONAL), FakeClock(1000.0)
    reader = StatusReader(fetch, clock)
    reader.read()
    fetch.payload = ACTIONS_DOWN
    clock.now = 1300.0
    status = reader.read()
    assert fetch.calls == 2
    assert (status.degraded, status.fetched_at) == (True, 1300.0)


def test_fetch_failure_falls_back_to_cache_and_retries() -> None:
    fetch, clock = FakeFetch(ACTIONS_DOWN), FakeClock(1000.0)
    reader = StatusReader(fetch, clock)
    good = reader.read()
    fetch.fail = True
    clock.now = 2000.0
    assert reader.read() == good
    assert reader.read() == good
    assert fetch.calls == 3


def test_fetch_failure_without_cache_is_not_degraded() -> None:
    fetch = FakeFetch(OPERATIONAL)
    fetch.fail = True
    assert StatusReader(fetch, FakeClock(7.0)).read() == ForgeStatus(False, "", 7.0)
