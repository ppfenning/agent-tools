import copy
import os
import signal
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_tools import chair_exec, chair_report, store_cli
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_run import DEFAULT_INTERVAL, RunDeps, as_holder, error_line, land_sink, run, tick

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)  # 14:05 EDT
MINE = {"holder": "me", "host": "box", "epoch": 3, "mine": True, "released": False, "stale": False}
FOREIGN = {**MINE, "holder": "other", "mine": False}
FREE = {**MINE, "mine": False, "released": True}


def _meter_doc(age_minutes):
    return {
        "five_hour": {"used_percentage": 42, "resets_at": 1798650300},
        "seven_day": {"used_percentage": 7, "resets_at": 1799000000},
        "observed_at": (NOW - timedelta(minutes=age_minutes)).isoformat(),
    }


def _facts(lease):
    return {
        "lease": lease,
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 4, "live_runs": 0, "hosts": []},
        "approved": [
            {
                "id": "t1", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [],
                "run": "x-1", "needs_fetch": False,
            }
        ],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": False,
        "sources_configured": False,
        "last_housekeeping_at": NOW.isoformat(),
        "housekeeping_hours": 24,
    }


class Rig:
    """Fakes for every dep. `lease` is the store row `gather` reads; a losing `beat` overwrites it, as a lost renewal would."""

    def __init__(self, lease=MINE, sleeps_before_interrupt=1, beat_loses=False):
        self.log, self.lines, self.commands, self.acquired, self.sleeps, self.released = [], [], [], [], [], []
        self.recorded = []
        self.lease, self.limit, self.beat_loses, self.holding = lease, sleeps_before_interrupt, beat_loses, True

    def _gather(self, _deps, _now):
        self.log.append("gather")
        return _facts(self.lease)

    def _beat(self):
        self.log.append("beat")
        self.lease = FOREIGN if self.beat_loses else self.lease

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) >= self.limit:
            raise KeyboardInterrupt

    def _run(self, argv):
        self.commands.append(argv)
        return 0, "merge: ok mark_done: ok"

    def _acquire(self, holder, host):
        self.acquired.append((holder, host))
        return ""

    def deps(self, echo=None, notify=None) -> RunDeps:
        exec_deps = chair_exec.Deps(
            run=self._run,
            delete_branches=lambda repo, pattern: ([], ""),
            acquire_lease=self._acquire,
            record=self.recorded.append,
            run_id=lambda action: "run-1",
            repo_for=lambda action: "r",
        )
        return RunDeps(
            facts_deps=object(),
            exec_deps=exec_deps,
            report_deps=chair_report.Deps(echo=echo or self.lines.append, notify=notify),
            beat=self._beat,
            current_epoch=lambda: 3,
            holds=lambda: self.holding,
            release=lambda: self.released.append(True),
            sleep=self._sleep,
            now=lambda: NOW,
            gather=self._gather,
        )


def test_once_runs_one_tick_that_lands_through_the_planner_and_never_sleeps():
    rig = Rig()
    assert run(True, 60, False, rig.deps()) is None
    assert rig.log == ["beat", "gather"]
    assert len(rig.commands) == 1 and "lands 1" in rig.lines[0]
    assert len(rig.lines) == 1 and rig.sleeps == []


def test_a_renewal_lost_in_the_beat_yields_a_standby_line_and_no_land():
    rig = Rig(lease=MINE, beat_loses=True)
    run(True, 60, False, rig.deps())
    assert "standby holder=other host=box" in rig.lines[0]
    assert "lands 0" in rig.lines[0]
    assert rig.commands == []


def test_the_first_tick_acquires_a_free_lease():
    rig = Rig(lease=FREE)
    run(True, 60, False, rig.deps())
    assert rig.acquired == [("", "")]


def test_dry_run_passes_through_and_touches_nothing():
    rig = Rig(lease=FREE)
    run(True, 60, True, rig.deps())
    assert " | dry-run | " in rig.lines[0]
    assert rig.acquired == [] and rig.commands == []


def _dry_run_plan(lease, **over):
    """The action kinds a dry-run tick plans through the real planner, and the rig that performed none of them."""
    rig, planned = Rig(lease=lease), []
    facts = {**_facts(lease), **over}

    def plan(f, n):
        planned.extend(plan_tick(f))
        return planned

    run(True, 60, True, replace(rig.deps(), gather=lambda d, n: facts, plan=plan))
    return [a["kind"] for a in planned], rig


def test_a_dry_run_on_a_released_lease_plans_the_land_the_holder_would():
    kinds, rig = _dry_run_plan(FREE)
    assert kinds == ["land_phase"]
    assert rig.acquired == [] and rig.commands == []


def test_a_dry_run_on_a_stale_lease_plans_the_launch_the_holder_would():
    fresh = {"id": "i", "started": False, "ready_tasks": [{"id": "t", "needs": []}], "landed": set()}
    kinds, rig = _dry_run_plan({**FREE, "released": False, "stale": True}, approved=[], initiatives=[fresh])
    assert kinds == ["launch_epic"]
    assert rig.acquired == [] and rig.commands == []


def test_a_dry_run_behind_a_live_foreign_holder_still_plans_standby():
    kinds, _rig = _dry_run_plan(FOREIGN)
    assert kinds == ["standby"]


def test_a_live_tick_on_a_released_lease_still_plans_take_lease():
    rig, planned = Rig(lease=FREE), []
    run(True, 60, False, replace(rig.deps(), plan=lambda f, n: planned.extend(plan_tick(f)) or planned))
    assert [a["kind"] for a in planned] == ["take_lease"]


def test_as_holder_wins_a_released_or_stale_lease_without_mutating_its_input():
    for lease in (FREE, {**FREE, "released": False, "stale": True}):
        facts = _facts(lease)
        before = copy.deepcopy(facts)
        assert as_holder(facts)["lease"]["mine"] is True
        assert facts == before


def test_as_holder_leaves_a_live_foreign_or_own_lease_as_it_is():
    for lease in (FOREIGN, MINE):
        facts = _facts(lease)
        assert as_holder(facts) == facts


def test_an_exception_in_one_tick_does_not_stop_the_next():
    rig = Rig(sleeps_before_interrupt=2)
    calls = []

    def flaky(deps, now):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        return rig._gather(deps, now)

    run(False, DEFAULT_INTERVAL, False, replace(rig.deps(), gather=flaky))
    assert rig.lines[0] == "chair 09-26 14:05 EDT | tick error: ValueError: boom"
    assert "holding" in rig.lines[1]
    assert rig.log == ["beat", "beat", "gather"]
    assert rig.sleeps == [60.0, 60.0]


def test_a_failure_after_perform_names_what_was_performed():
    rig = Rig()
    land = {"kind": "land", "task_id": "t1", "repo": "r", "run": "x-1", "epoch": 3}
    no_dispatch = {k: v for k, v in _facts(MINE).items() if k != "dispatch"}
    run(True, 60, False, replace(rig.deps(), gather=lambda d, n: no_dispatch, plan=lambda f, n: [land]))
    assert len(rig.commands) == 1
    assert rig.lines == ["chair 09-26 14:05 EDT | tick error: KeyError: 'dispatch' | performed: land:landed"]


def test_a_raising_notify_is_echoed_and_the_next_tick_still_runs():
    rig = Rig(sleeps_before_interrupt=2)

    def notify(_note):
        raise FileNotFoundError("notify-send")

    run(False, 60, False, rig.deps(notify=notify))
    assert rig.lines[1] == "chair 09-26 14:05 EDT | tick error: FileNotFoundError: notify-send"
    assert rig.log == ["beat", "gather", "beat", "gather"]
    assert len(rig.lines) == 4


def test_a_raising_echo_does_not_stop_the_loop():
    rig = Rig(sleeps_before_interrupt=2)

    def echo(_line):
        raise OSError("stdout closed")

    run(False, 60, False, rig.deps(echo=echo))
    assert rig.log == ["beat", "gather", "beat", "gather"]
    assert rig.released == [True]


def test_error_line_is_one_literal_line():
    assert error_line(KeyError("k"), NOW) == "chair 09-26 14:05 EDT | tick error: KeyError: 'k'"


def test_interrupt_releases_the_lease_only_when_held():
    held, not_held = Rig(), Rig()
    not_held.holding = False
    run(False, 60, False, held.deps())
    run(False, 60, False, not_held.deps())
    assert held.released == [True] and not_held.released == []


def test_sigterm_with_a_pending_land_releases_its_lease_and_returns(monkeypatch):
    held, gate = {}, threading.Event()
    monkeypatch.setattr(store_cli, "lease_acquire", lambda d, name, holder, ttl, steal=False: held.setdefault(name, holder) and store_cli.LeaseGranted(1, holder))
    monkeypatch.setattr(store_cli, "lease_release", lambda d, name, holder, epoch: held.pop(name) and store_cli.LeaseReleased())
    lands = land_sink(Path("/runs"), "me", lambda: None, wait=lambda s: gate.wait(0.01))
    assert lands.submit({"kind": "land", "repo": "/repo"}, lambda: gate.wait(5))["status"] == "in_progress"
    rig = Rig()
    rig._sleep = lambda seconds: os.kill(os.getpid(), signal.SIGTERM)
    assert run(False, 60, False, replace(rig.deps(), stop_lands=lands.stop)) is None
    assert held == {} and rig.released == [True]
    gate.set()


def test_a_dry_run_installs_no_sigterm_handler():
    installed = []
    rig = Rig(lease=FREE)
    run(True, 60, True, replace(rig.deps(), install_sigterm=lambda: installed.append(True) or (lambda: None)))
    assert installed == []


def _meter_actions(rig):
    return [a for a in rig.recorded if a.get("kind") == "meter"]


def test_a_fresh_meter_reading_is_recorded_once_with_the_docs_fields():
    rig, doc = Rig(), _meter_doc(5)
    tick(replace(rig.deps(), meter_doc=lambda: doc), False, NOW)
    (action,) = _meter_actions(rig)
    assert action["status"] == "recorded"
    assert (action["five_hour"], action["seven_day"], action["observed_at"]) == (
        doc["five_hour"], doc["seven_day"], doc["observed_at"],
    )


@pytest.mark.parametrize("reading", [None, _meter_doc(16)], ids=["missing", "16-minutes-old"])
def test_a_missing_or_stale_meter_reading_records_no_meter_action(reading):
    rig = Rig()
    tick(replace(rig.deps(), meter_doc=lambda: reading), False, NOW)
    assert _meter_actions(rig) == []


def test_a_dry_run_records_no_meter_action_even_when_fresh():
    rig = Rig()
    tick(replace(rig.deps(), meter_doc=lambda: _meter_doc(5)), True, NOW)
    assert _meter_actions(rig) == []


def test_a_gather_that_raises_still_publishes_the_meter_once():
    rig = Rig()

    def boom(_deps, _now):
        raise RuntimeError("gather down")

    deps = replace(rig.deps(), gather=boom, meter_doc=lambda: _meter_doc(5))
    with pytest.raises(RuntimeError):
        tick(deps, False, NOW)
    assert len(_meter_actions(rig)) == 1


def test_a_tick_records_one_status_row_with_its_line():
    rig = Rig()
    run(True, 60, False, rig.deps())
    (row,) = [a for a in rig.recorded if a.get("kind") == "status"]
    assert row["line"] == rig.lines[0] and row["status"] == "recorded"


def test_a_dry_run_records_no_status_row():
    rig = Rig(lease=FREE)
    run(True, 60, True, rig.deps())
    assert [a for a in rig.recorded if a.get("kind") == "status"] == []


def test_a_failing_meter_read_or_record_cannot_break_the_tick():
    rig = Rig()

    def read_fails():
        raise OSError("gone")

    tick(replace(rig.deps(), meter_doc=read_fails), False, NOW)
    assert rig.log == ["beat", "gather"]
