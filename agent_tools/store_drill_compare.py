"""Compare a restored store snapshot against the live one; pure, no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["TABLES", "CompareResult", "Finding", "Snapshot", "check_count", "check_schema", "compare"]

TABLES = ("runs", "work_items", "chair_actions")


@dataclass(frozen=True)
class Snapshot:
    schema_version: str
    counts: Mapping[str, int]


@dataclass(frozen=True)
class Finding:
    check: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CompareResult:
    passed: bool
    findings: tuple[Finding, ...]


def check_schema(restored: Snapshot, live: Snapshot) -> Finding:
    ok = restored.schema_version == live.schema_version
    return Finding("schema_version", ok, f"restored={restored.schema_version!r} live={live.schema_version!r}")


def check_count(table: str, restored: int | None, live: int | None, tolerance: float) -> Finding:
    """Restored may trail live by at most `tolerance` as a fraction of live, and never exceed it."""
    if restored is None or live is None:
        return Finding(table, False, f"missing table: restored={restored} live={live}")
    if live == 0:
        return Finding(table, restored == 0, f"live is empty: restored={restored}")
    if restored > live:
        return Finding(table, False, f"restored {restored} is above live {live}")
    ok = live - restored <= tolerance * live
    return Finding(table, ok, f"restored={restored} live={live} tolerance={tolerance}")


def compare(restored: Snapshot, live: Snapshot, tolerance: float) -> CompareResult:
    findings = (
        check_schema(restored, live),
        *(check_count(t, restored.counts.get(t), live.counts.get(t), tolerance) for t in TABLES),
    )
    return CompareResult(all(f.ok for f in findings), findings)
