"""The revert watch store, over an in-memory stand-in for the chair's recorder and the store's record-action."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_tools.chair_revert_watch import outcomes, pending_watches, record_watch, resolve_watch
from agent_tools.chair_types import LandWatch


def _fake() -> tuple[Callable[[Mapping[str, Any]], None], Callable[[], list[dict[str, Any]]]]:
    """The recorder adds a rising ts and an epoch; the store refuses a line without ts, epoch, kind and status."""
    rows: list[dict[str, Any]] = []

    def write(action: Mapping[str, Any]) -> None:
        line = {"ts": f"2026-10-06T00:00:{len(rows):02d}Z", "epoch": 1, **action}
        missing = {"ts", "epoch", "kind", "status"} - {k for k, v in line.items() if v}
        if missing:
            raise AssertionError(f"store refuses the line, missing {sorted(missing)}")
        rows.append({"kind": line["kind"], "ts": line["ts"], "action_json": json.dumps(line)})

    return write, lambda: list(rows)


def _watch(commit: str, initiative: str = "alpha") -> LandWatch:
    return LandWatch(initiative=initiative, phase="p1", repo="acme/app", pr=7, commit=commit)


def test_recorded_watch_is_pending() -> None:
    write, read = _fake()
    record_watch(write, _watch("abc123"))
    assert pending_watches(read) == [
        {"initiative": "alpha", "phase": "p1", "repo": "acme/app", "pr": 7, "commit": "abc123"}
    ]


def test_same_commit_recorded_twice_is_one_pending_watch() -> None:
    write, read = _fake()
    record_watch(write, _watch("abc123"))
    record_watch(write, _watch("abc123"))
    assert [w["commit"] for w in pending_watches(read)] == ["abc123"]


def test_resolved_watch_is_no_longer_pending() -> None:
    write, read = _fake()
    record_watch(write, _watch("abc123"))
    record_watch(write, _watch("def456"))
    resolve_watch(write, "abc123", "held")
    assert [w["commit"] for w in pending_watches(read)] == ["def456"]


def test_outcomes_for_one_initiative_come_back_in_resolution_order() -> None:
    write, read = _fake()
    record_watch(write, _watch("c1"))
    record_watch(write, _watch("c2"))
    resolve_watch(write, "c2", "reverted")
    resolve_watch(write, "c1", "held")
    assert outcomes(read) == {"alpha": ["reverted", "held"]}


def test_outcomes_keep_two_initiatives_apart() -> None:
    write, read = _fake()
    record_watch(write, _watch("c1", "alpha"))
    record_watch(write, _watch("c2", "beta"))
    resolve_watch(write, "c1", "held")
    resolve_watch(write, "c2", "reverted")
    assert outcomes(read) == {"alpha": ["held"], "beta": ["reverted"]}


def test_a_second_verdict_for_a_commit_is_ignored() -> None:
    write, read = _fake()
    record_watch(write, _watch("c1"))
    resolve_watch(write, "c1", "held")
    resolve_watch(write, "c1", "reverted")
    assert outcomes(read) == {"alpha": ["held"]}


def test_outcomes_order_mixed_ts_formats_and_break_ties_by_commit() -> None:
    watch = {"kind": "revert_watch", "initiative": "alpha", "phase": "p1", "repo": "acme/app", "pr": 7}
    watched = [{**watch, "commit": c, "ts": "2026-10-06T00:00:00Z"} for c in ("c1", "c2", "c3")]
    resolved = [
        {"kind": "revert_watch_resolved", "commit": "c3", "outcome": "held", "ts": "2026-10-06T00:00:09Z"},
        {"kind": "revert_watch_resolved", "commit": "c2", "outcome": "reverted", "ts": "2026-10-06T00:00:05+00:00"},
        {
            "kind": "revert_watch_resolved",
            "commit": "c1",
            "outcome": "held",
            "ts": datetime(2026, 10, 6, 0, 0, 5, tzinfo=UTC),
        },
    ]
    assert outcomes(lambda: watched + resolved) == {"alpha": ["held", "reverted", "held"]}


def test_an_unknown_outcome_is_refused() -> None:
    write, _ = _fake()
    with pytest.raises(ValueError):
        resolve_watch(write, "c1", "ignored")
