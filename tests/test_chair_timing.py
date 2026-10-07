from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime

from agent_tools import chair_exec, chair_report, chair_timing
from agent_tools.chair_facts import FactsDeps
from agent_tools.chair_run import RunDeps, _attempt, tick_timed

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)


class FakeClock:
    """Each reading returns the next value, so a timed call lasts the gap between two readings."""

    def __init__(self, *readings: float) -> None:
        self.readings = list(readings)

    def __call__(self) -> float:
        return self.readings.pop(0)


def test_timed_returns_the_value_and_the_seconds_between_two_readings() -> None:
    assert chair_timing.timed(FakeClock(10.0, 10.25), lambda a, b=0: a + b, 1, b=2) == (3, 0.25)


def test_format_timing_prints_literal_seconds_slowest_first_and_five_at_most() -> None:
    stages = {"facts": 1.5, "plan": 0.25, "perform": 0.0005, "export": None}
    fields = {"a": 0.1, "b": 0.9, "c": 0.5, "d": 0.5, "e": 0.2, "f": 0.01}
    assert chair_timing.format_timing(stages, fields) == (
        "timing: facts=1.500s plan=0.250s perform=0.001s export=skipped | slowest: b=0.900s, c=0.500s, d=0.500s, e=0.200s, a=0.100s"
    )


def test_a_stage_that_ran_in_no_time_prints_zero_and_one_that_did_not_run_prints_skipped() -> None:
    line = chair_timing.format_timing({"facts": 0.0, "plan": 0.0, "perform": 0.0, "export": None}, {})
    assert line == "timing: facts=0.000s plan=0.000s perform=0.000s export=skipped | slowest: none"


def test_ran_drops_the_stages_that_did_not_run_and_rounds_to_the_millisecond() -> None:
    assert chair_timing.ran({"facts": 1.23456, "plan": 0.0, "perform": 2.0, "export": None}) == {"facts": 1.235, "plan": 0.0, "perform": 2.0}


def test_every_callable_facts_deps_field_is_wrapped_and_returns_what_it_returned() -> None:
    base = _facts_deps()
    sink: dict[str, float] = {}
    wrapped = chair_timing.wrap_callables(base, lambda: 0.0, sink)
    for f in dataclasses.fields(base):
        original = getattr(base, f.name)
        if callable(original):
            assert getattr(wrapped, f.name) is not original, f.name
            assert getattr(wrapped, f.name)(1, k=2) == (f.name, (1,), {"k": 2}), f.name
        else:
            assert getattr(wrapped, f.name) is original, f.name
    assert set(sink) == {f.name for f in dataclasses.fields(base) if callable(getattr(base, f.name))}


def test_a_wrapped_field_adds_its_seconds_to_the_sink_on_every_call() -> None:
    sink: dict[str, float] = {}
    wrapped = chair_timing.wrap_callables(_facts_deps(), FakeClock(0.0, 0.5, 1.0, 1.25), sink)
    wrapped.lease()
    wrapped.lease()
    assert sink == {"lease": 0.75}


def test_a_value_that_is_not_a_dataclass_comes_back_unchanged() -> None:
    thing = object()
    assert chair_timing.wrap_callables(thing, lambda: 0.0, {}) is thing


def _echo(name: str):
    return lambda *args, **kwargs: (name, args, kwargs)


def _facts_deps() -> FactsDeps:
    """Every callable field, defaulted or required, replaced by a fake that returns its own name and arguments."""
    required = {f.name: _echo(f.name) for f in dataclasses.fields(FactsDeps) if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING}
    built = FactsDeps(**required)
    return replace(built, **{f.name: _echo(f.name) for f in dataclasses.fields(built) if callable(getattr(built, f.name))})


def _rig(echoed: list[str], recorded: list[dict], export_rows=None) -> RunDeps:
    exec_deps = chair_exec.Deps(
        run=lambda argv: (0, "merge: ok mark_done: ok"),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "r",
    )
    return RunDeps(
        facts_deps=_facts_deps(),
        exec_deps=exec_deps,
        report_deps=chair_report.Deps(echo=echoed.append),
        beat=lambda: None,
        current_epoch=lambda: 3,
        holds=lambda: True,
        release=lambda: None,
        sleep=lambda _s: None,
        now=lambda: NOW,
        gather=lambda facts_deps, now: _gathered(facts_deps),
        plan=lambda facts, now: [{"kind": "land", "task_id": "t1", "repo": "r", "run": "x-1", "epoch": 3}],
        export_rows=export_rows,
    )


def _gathered(facts_deps: FactsDeps) -> dict:
    facts_deps.lease()
    facts_deps.lease()
    facts_deps.docket()
    return {
        "lease": {"holder": "chair", "host": "h", "epoch": 3, "released": False, "stale": False},
        "dispatch": {"max_in_flight": 3, "hosts": []},
        "limits": {},
        "intake": [],
        "approved": [],
        "stranded": [],
        "quarantined": [],
    }


def test_tick_timed_reports_each_stage_and_the_facts_sources_from_its_clock() -> None:
    clock = FakeClock(*[float(n) for n in range(100)])
    base = _rig([], [])
    _line, stages, sources = tick_timed(replace(base, gather=lambda fd, now: _gathered(fd)), True, NOW, clock)
    assert stages["export"] is None
    assert sources == {"lease": 2.0, "docket": 1.0}
    assert {k: v for k, v in stages.items() if k != "export"} == {"facts": 7.0, "plan": 1.0, "perform": 1.0}


def test_a_dry_run_echoes_the_timing_line_first_with_export_skipped_and_records_nothing() -> None:
    echoed: list[str] = []
    recorded: list[dict] = []
    _attempt(_rig(echoed, recorded), True)
    assert echoed[0].startswith("timing: facts=") and " export=skipped | slowest: " in echoed[0]
    assert len(echoed) == 2 and not echoed[1].startswith("timing: ")
    assert [a for a in recorded if a.get("kind") == "status"] == []


def test_a_live_tick_records_stage_seconds_on_its_status_row() -> None:
    echoed: list[str] = []
    recorded: list[dict] = []
    _attempt(_rig(echoed, recorded), False)
    (row,) = [a for a in recorded if a.get("kind") == "status"]
    assert set(row["stage_seconds"]) == {"facts", "plan", "perform"}
    assert row["line"] == echoed[1]


def test_a_live_tick_that_exports_records_the_export_stage_too() -> None:
    echoed: list[str] = []
    recorded: list[dict] = []
    _attempt(_rig(echoed, recorded, export_rows=lambda ids: ""), False)
    (row,) = [a for a in recorded if a.get("kind") == "status"]
    assert set(row["stage_seconds"]) == {"facts", "plan", "perform", "export"}
    assert "export=skipped" not in echoed[0]


def test_a_tick_that_raises_echoes_only_its_error_line() -> None:
    echoed: list[str] = []

    def boom(_fd, _now):
        raise RuntimeError("gather down")

    _attempt(replace(_rig(echoed, []), gather=boom), True)
    assert echoed == ["chair 09-26 14:05 EDT | tick error: RuntimeError: gather down"]
