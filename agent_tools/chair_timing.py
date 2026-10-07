"""Pure timing helpers for the chair tick: a clock goes in, plain seconds come out."""
from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["STAGES", "TOP_FIELDS", "Clock", "Stages", "format_timing", "ran", "timed", "wrap_callables"]

Clock = Callable[[], float]
Stages = Mapping[str, float | None]  # a stage that did not run maps to None

STAGES = ("facts", "plan", "perform", "export")
TOP_FIELDS = 5


def timed[T](clock: Clock, fn: Callable[..., T], *args: Any, **kwargs: Any) -> tuple[T, float]:
    """The call's value and the seconds `clock` counted around it. A call that raises is not timed."""
    start = clock()
    value = fn(*args, **kwargs)
    return value, clock() - start


def _accumulating(name: str, fn: Callable[..., Any], clock: Clock, sink: dict[str, float]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def call(*args: Any, **kwargs: Any) -> Any:
        value, seconds = timed(clock, fn, *args, **kwargs)
        sink[name] = sink.get(name, 0.0) + seconds
        return value

    return call


def wrap_callables(deps: Any, clock: Clock, sink: dict[str, float]) -> Any:
    """`deps` with every callable dataclass field timed into `sink` (field name to summed seconds).

    `sink` is the caller's out-parameter. Fields are found with `dataclasses.fields`, so a new source is timed unasked.
    A value that is not a dataclass instance comes back unchanged.
    """
    if not dataclasses.is_dataclass(deps) or isinstance(deps, type):
        return deps
    timed_fields = {f.name: _accumulating(f.name, v, clock, sink) for f in dataclasses.fields(deps) if callable(v := getattr(deps, f.name))}
    return dataclasses.replace(deps, **timed_fields)


def _seconds(value: float | None) -> str:
    return "skipped" if value is None else f"{value:.3f}s"


def format_timing(stages: Stages, fields: Mapping[str, float], top: int = TOP_FIELDS) -> str:
    """The `timing:` line. Slowest fields come first, ties by name."""
    head = " ".join(f"{name}={_seconds(stages.get(name))}" for name in STAGES)
    slowest = sorted(fields.items(), key=lambda item: (-item[1], item[0]))[:top]
    tail = ", ".join(f"{name}={_seconds(seconds)}" for name, seconds in slowest) or "none"
    return f"timing: {head} | slowest: {tail}"


def ran(stages: Stages) -> dict[str, float]:
    """The stages that ran, in seconds to the millisecond; the `stage_seconds` of an action record."""
    return {name: round(seconds, 3) for name in STAGES if (seconds := stages.get(name)) is not None}
