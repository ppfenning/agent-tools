import copy
import os
import signal
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools import chair_exec, chair_report
from agent_tools.chair_plan import TickPlan, plan_tick
from agent_tools.chair_run import (
    DEFAULT_INTERVAL,
    PROFILE_STALE_NOTE,
    RunDeps,
    as_holder,
    error_line,
    land_sink,
    next_gate_record,
    no_landing,
    run,
    tick,
)
from agent_tools.ci_gate import CiGate
from agent_tools.forge_status import StatusReader
from agent_tools.notify_core import NotifyConfig
from agent_tools.notify_dispatch import dispatch

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
    assert len(rig.commands) == 1 and "lands 1" in rig.lines[1]
    assert len(rig.lines) == 2 and rig.lines[0].startswith("timing: ") and rig.sleeps == []


def test_a_renewal_lost_in_the_beat_yields_a_standby_line_and_no_land():
    rig = Rig(lease=MINE, beat_loses=True)
    run(True, 60, False, rig.deps())
    assert "standby holder=other host=box" in rig.lines[1]
    assert "lands 0" in rig.lines[1]
    assert rig.commands == []


def test_the_first_tick_acquires_a_free_lease():
    rig = Rig(lease=FREE)
    run(True, 60, False, rig.deps())
    assert rig.acquired == [("", "")]


def test_dry_run_passes_through_and_touches_nothing():
    rig = Rig(lease=FREE)
    run(True, 60, True, rig.deps())
    assert " | dry-run | " in rig.lines[1]
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
    assert "holding" in rig.lines[2]
    assert rig.log == ["beat", "beat", "gather"]
    assert rig.sleeps == [60.0, 60.0]


def test_a_failure_after_perform_names_what_was_performed():
    rig = Rig()
    land = {"kind": "land", "task_id": "t1", "repo": "r", "run": "x-1", "epoch": 3}
    no_dispatch = {k: v for k, v in _facts(MINE).items() if k != "dispatch"}
    run(True, 60, False, replace(rig.deps(), gather=lambda d, n: no_dispatch, plan=lambda f, n: [land]))
    assert len(rig.commands) == 1
    assert rig.lines[1:] == ["chair 09-26 14:05 EDT | tick error: KeyError: 'dispatch' | performed: land:landed"]


def test_a_raising_notify_is_echoed_and_the_next_tick_still_runs():
    rig = Rig(sleeps_before_interrupt=2)

    def notify(_note):
        raise FileNotFoundError("notify-send")

    run(False, 60, False, rig.deps(notify=notify))
    assert rig.lines[2] == "chair 09-26 14:05 EDT | tick error: FileNotFoundError: notify-send"
    assert rig.log == ["beat", "gather", "beat", "gather"]
    assert len(rig.lines) == 6


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


def test_sigterm_with_a_pending_land_stops_the_worker_and_returns():
    gate = threading.Event()
    lands = land_sink(lambda: None, wait=lambda s: gate.wait(0.01))
    assert lands.submit({"kind": "land", "repo": "/repo"}, lambda: gate.wait(5))["status"] == "in_progress"
    rig = Rig()
    rig._sleep = lambda seconds: os.kill(os.getpid(), signal.SIGTERM)
    assert run(False, 60, False, replace(rig.deps(), stop_lands=lands.stop)) is None
    assert lands.submit({"kind": "land", "repo": "/other"}, lambda: None)["status"] == "busy"
    assert rig.released == [True]
    gate.set()


def test_landing_lists_a_land_in_progress_and_drops_it_once_it_finishes():
    release = threading.Event()
    lands = land_sink(lambda: None, wait=lambda s: release.wait(0.01))
    action = {"kind": "land_phase", "initiative": "ind", "phase": "p1", "repo": "/repo", "run": "ind-1"}
    assert lands.submit(action, lambda: release.wait(5) and {"action": action, "status": "ok", "reason": ""})["status"] == "in_progress"
    assert lands.landing() == [{"initiative": "ind", "phase": "p1", "repo": "/repo"}]
    release.set()
    assert lands._handles["/repo"][1].wait(5)
    assert lands.landing() == []
    assert len(lands.collect()) == 1 and lands.landing() == []


def test_a_chair_with_no_land_worker_reports_no_landing():
    assert no_landing() == []


def test_a_dry_run_installs_no_sigterm_handler():
    installed = []
    rig = Rig(lease=FREE)
    run(True, 60, True, replace(rig.deps(), install_sigterm=lambda: installed.append(True) or (lambda: None)))
    assert installed == []


class Forge:
    """A fake status endpoint and its clock, in epoch seconds."""

    def __init__(self):
        self.at, self.down, self.fetches = NOW.timestamp(), False, 0

    def fetch(self):
        self.fetches += 1
        status = "major_outage" if self.down else "operational"
        return {"components": [{"name": "Actions", "status": status}], "incidents": [{"name": "Actions outage"}]}

    def reader(self):
        return StatusReader(self.fetch, lambda: self.at)

    def tick(self, deps, seconds=60):
        self.at += seconds
        return tick(deps, False, datetime.fromtimestamp(self.at, UTC))


class Performed:
    """A perform that keeps the actions it was given, one list per tick."""

    def __init__(self):
        self.ticks = []

    def __call__(self, actions, _deps, _epoch, _dry_run):
        self.ticks.append([a["kind"] + ":" + a.get("initiative", "") for a in actions])
        return []


def _gated(rig, forge, performed=None, **over):
    """Dependents `dep` and an independent `ind`; a pause drops the relaunch of `dep` and names it held."""
    def plan(_facts, _now):
        return [{"kind": "relaunch", "initiative": "dep", "epoch": 3}, {"kind": "launch_epic", "initiative": "ind", "epoch": 3}]

    def plan_held(_facts, _now, _gate):
        return TickPlan([{"kind": "launch_epic", "initiative": "ind", "epoch": 3}], ("dep",))

    fields = {"status_reader": forge.reader(), "plan": plan, "plan_held": plan_held}
    return replace(rig.deps(), **({"perform": performed} if performed else {}), **fields, **over)


def _needs_chair(rig):
    return [a for a in rig.recorded if a.get("kind") == "needs_chair"]


def test_a_degraded_forge_holds_the_dependent_relaunch_while_an_independent_launch_continues():
    forge, performed = Forge(), Performed()
    deps = _gated(Rig(), forge, performed)
    forge.tick(deps)
    forge.down = True
    forge.tick(deps, 400)
    assert performed.ticks == [["relaunch:dep", "launch_epic:ind"], ["launch_epic:ind", "needs_chair:ci-gate"]]


def test_recovery_on_a_later_tick_resumes_the_relaunch_with_no_extra_record():
    forge, performed = Forge(), Performed()
    deps = _gated(Rig(), forge, performed)
    forge.down = True
    forge.tick(deps)
    forge.down = False
    forge.tick(deps, 400)
    assert performed.ticks[1] == ["relaunch:dep", "launch_epic:ind"]


def test_repeated_degraded_ticks_record_one_needs_chair_naming_the_reason_and_the_held_initiative():
    rig, forge = Rig(), Forge()
    deps = _gated(rig, forge)
    forge.down = True
    for _ in range(3):
        forge.tick(deps, 400)
    [recorded] = _needs_chair(rig)
    assert "Actions outage" in recorded["cause"] and "held for CI: dep" in recorded["cause"]
    assert recorded["status"] == "recorded"
    forge.down = False
    forge.tick(deps, 400)
    assert len(_needs_chair(rig)) == 1
    forge.down = True
    forge.tick(deps, 400)
    assert len(_needs_chair(rig)) == 2


def test_a_degraded_tick_holds_the_land_and_makes_no_merge_until_the_forge_recovers():
    rig, forge = Rig(), Forge()
    ev = threading.Event()
    sink = land_sink(lambda: None, wait=lambda s: ev.wait(0.01))
    deps = replace(
        rig.deps(), status_reader=forge.reader(), set_gate=sink.set_gate, queued_since=sink.queued_since,
        exec_deps=replace(rig.deps().exec_deps, lands=sink),
    )
    forge.down = True
    forge.tick(deps)
    assert rig.commands == [] and sink.pending("r")
    forge.down = False
    forge.tick(deps, 400)
    sink._handles["r"][1].wait(5)
    assert len(rig.commands) == 1
    assert len(_needs_chair(rig)) == 1


def test_a_land_queued_past_the_bound_pauses_an_operational_forge_and_resumes_when_its_checks_start():
    rig, forge, queued = Rig(), Forge(), [NOW.timestamp() - 1801 + 60]
    performed = Performed()
    deps = _gated(rig, forge, performed, queued_since=lambda: tuple(queued))
    forge.tick(deps)
    queued.clear()
    forge.tick(deps, 10)
    assert performed.ticks[0] == ["launch_epic:ind", "needs_chair:ci-gate"]
    assert performed.ticks[1] == ["relaunch:dep", "launch_epic:ind"]


def test_the_endpoint_is_fetched_once_across_ticks_inside_five_minutes():
    forge = Forge()
    deps = _gated(Rig(), forge, Performed())
    forge.tick(deps)
    forge.tick(deps)
    forge.tick(deps, 120)
    assert forge.fetches == 1
    forge.tick(deps, 301)
    assert forge.fetches == 2


def test_a_gate_records_only_on_the_tick_that_pauses_it():
    paused = CiGate(True, "outage")
    assert next_gate_record(False, paused, ("a", "b"), 3) == {
        "kind": "needs_chair", "initiative": "ci-gate", "cause": "ci_gate_paused: outage; held for CI: a, b", "epoch": 3,
    }
    assert next_gate_record(True, paused, ("a",), 3) is None
    assert next_gate_record(True, CiGate(False, ""), (), 3) is None


def test_a_dry_run_records_no_gate_and_leaves_the_carried_state_alone():
    rig, forge = Rig(lease=FREE), Forge()
    deps = _gated(rig, forge)
    forge.down = True
    forge.at += 60
    tick(deps, True, datetime.fromtimestamp(forge.at, UTC))
    assert _needs_chair(rig) == [] and deps.gate_state.paused is False


@pytest.mark.parametrize("gated", [False, True])
def test_an_unpaused_tick_reads_no_lease_from_the_facts_and_keeps_the_carried_state(gated):
    forge, seen = Forge(), []
    deps = replace(
        Rig().deps(), gather=lambda _d, _n: {}, plan=lambda f, n: seen.append(f) or [],
        perform=lambda *_: [], **({"status_reader": forge.reader()} if gated else {}),
    )
    forge.tick(deps)
    assert seen == [{}] and deps.gate_state.paused is False


def test_each_chair_carries_its_own_gate_state():
    assert Rig().deps().gate_state is not Rig().deps().gate_state


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


def test_a_dry_run_status_line_says_remote_lanes_were_not_probed_and_a_live_one_does_not():
    assert tick(Rig().deps(), True, NOW).endswith(f" | {chair_exec.NOT_PROBED}")
    assert chair_exec.NOT_PROBED not in tick(Rig().deps(), False, NOW)


def test_a_stale_profile_fact_adds_the_note_to_the_status_line_and_its_absence_does_not():
    rig = Rig()
    stale = replace(rig.deps(), gather=lambda d, n: {**_facts(MINE), "profile_stale": True})
    assert tick(stale, False, NOW).endswith(f" | {PROFILE_STALE_NOTE}")
    assert PROFILE_STALE_NOTE not in tick(Rig().deps(), False, NOW)


def test_the_written_status_line_carries_the_stale_profile_note():
    rig = Rig()
    run(True, 60, False, replace(rig.deps(), gather=lambda d, n: {**_facts(MINE), "profile_stale": True}))
    assert "profile re-read failed; keeping the last good profile" in rig.lines[-1]


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
    assert row["line"] == rig.lines[1] and row["status"] == "recorded"


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


STRANDED = {"kind": "needs_chair", "initiative": "ind", "cause": "stranded", "reason": "no live run"}
PUSH = NotifyConfig("https://ntfy.sh/t")


def _notify_deps(rig, tmp_path, notify, config=PUSH):
    return replace(
        rig.deps(), gather=lambda d, n: _facts(MINE), plan=lambda f, n: [STRANDED], perform=lambda a, d, e, dry: [],
        notify=notify, notify_config=config, workspace=tmp_path,
    )


def test_a_tick_hands_its_needs_chair_event_to_notify_with_the_config_state_path_and_now(tmp_path):
    calls = []
    deps = _notify_deps(Rig(), tmp_path, lambda *args: calls.append(args) or [])
    tick(deps, False, NOW)
    [(events, config, state_path, stamp)] = calls
    assert [e.key for e in events] == ["needs_chair:ind:stranded"]
    assert config == NotifyConfig("https://ntfy.sh/t")
    assert state_path == tmp_path / "notify-sent.json" and stamp == NOW.timestamp()


def test_a_notify_that_raises_is_written_as_a_status_line_and_the_next_tick_still_runs(tmp_path):
    calls = []

    def boom(*args):
        calls.append(args)
        raise OSError("ntfy down")

    rig = Rig(sleeps_before_interrupt=2)
    run(False, 60, False, _notify_deps(rig, tmp_path, boom))
    assert len(calls) == 2 and len(rig.lines) == 4
    assert all("notify failed: OSError: ntfy down" in line for line in rig.lines[1::2])


def test_a_dry_run_never_calls_notify(tmp_path):
    calls = []
    tick(_notify_deps(Rig(), tmp_path, lambda *args: calls.append(args) or []), True, NOW)
    assert calls == []


def test_a_profile_with_no_notify_config_dispatches_none_and_sends_nothing(tmp_path):
    seen, posts = [], []

    def through_dispatch(events, config, path, now):
        seen.append(config)
        return dispatch(events, config, path, now, post=lambda *a, **k: posts.append(a) or True)

    tick(_notify_deps(Rig(), tmp_path, through_dispatch, config=None), False, NOW)
    assert seen == [None] and posts == []
    assert not (tmp_path / "notify-sent.json").exists()
