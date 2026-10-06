from __future__ import annotations

import dataclasses

import pytest

from agent_tools.ci_gate import DEFAULT_QUEUED_BOUND_SECONDS, CiGate, evaluate_gate
from agent_tools.forge_status import ForgeStatus

OK = ForgeStatus(False, "", 0.0)
DOWN = ForgeStatus(True, "Actions degraded", 0.0)
NOW = 10000.0
BOUND = 1800.0


def test_operational_empty_queue_not_paused() -> None:
    assert evaluate_gate(OK, (), NOW, BOUND) == CiGate(False, "")


def test_degraded_paused_with_incident() -> None:
    assert evaluate_gate(DOWN, (), NOW, BOUND) == CiGate(True, "Actions degraded")


def test_one_second_under_bound_not_paused() -> None:
    assert evaluate_gate(OK, (NOW - 1799,), NOW, BOUND).paused is False


def test_exactly_at_bound_not_paused() -> None:
    assert evaluate_gate(OK, (NOW - 1800,), NOW, BOUND).paused is False


def test_one_second_over_bound_paused_with_count_and_minutes() -> None:
    gate = evaluate_gate(OK, (NOW - 1801, NOW - 5), NOW, BOUND)
    assert gate == CiGate(True, "1 land(s) waited past the 30-minute queued-checks bound")


def test_started_check_dropping_out_resumes() -> None:
    assert evaluate_gate(OK, (NOW - 1801,), NOW, BOUND).paused is True
    assert evaluate_gate(OK, (), NOW, BOUND) == CiGate(False, "")


def test_degraded_plus_stale_queue_reports_incident() -> None:
    assert evaluate_gate(DOWN, (NOW - 1801,), NOW, BOUND) == CiGate(True, "Actions degraded")


def test_default_bound_is_thirty_minutes() -> None:
    assert DEFAULT_QUEUED_BOUND_SECONDS == 1800


def test_gate_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CiGate(False, "").paused = True  # type: ignore[misc]
