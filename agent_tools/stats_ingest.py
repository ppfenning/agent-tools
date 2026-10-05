"""Loads the run corpus into the stats store (workspace/stats/stats.db).

Spec: docs/design/run-stats-store.md §3-§4. Spike verdict:
agent_tools/stats-join-spike.md — the calls-to-tasks join is heuristic: a
run's build calls get `task_id`/`join_confidence='heuristic'` in ticket order
only where `sum(fix_loop.attempts)` over its task records equals its build
call count (171/184 runs in the spike); every other call, in every other run,
gets `join_confidence='none'` and no `task_id` rather than a guess. Row-shaping
(`run_row`, `call_rows`, `recovered_call_rows`, `task_row`, `run_join_holds`,
`assign_task_ids`, `fill_failure_classes`) is pure over already-parsed JSON;
`load_run`, `discover_runs` and `ingest` are the edge that reads files
(including each call's own trace and the autonomy ledger) and writes the database.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.events import Event, from_log
from agent_tools.land import _ARBITER_SKIPPED, arbitration_verdict
from agent_tools.records import load_trace
from agent_tools.route import parse_frontmatter
from agent_tools.runs_detail import NODE_ORDER
from agent_tools.stats_derive import _final_result, attempt_numbers, extract_failure_class, resolve_outcome
from agent_tools.stats_schema import CALLS_COLUMNS, RUNS_COLUMNS, TASKS_COLUMNS, connect

__all__ = [
    "IngestReport",
    "assign_task_ids",
    "call_rows",
    "call_trace_key",
    "discover_runs",
    "fill_failure_classes",
    "ingest",
    "load_run",
    "provider_profile_for",
    "provider_profile_from_nodes",
    "provider_profile_source",
    "recovered_call_rows",
    "resolve_provider_profile_sha",
    "rollup_task_costs",
    "run_join_holds",
    "run_row",
    "task_row",
]

LEDGER_PATH = Path.home() / ".local" / "state" / "agent-graphs" / "ledger.jsonl"

SCHEMA_VERSION = 2


def run_row(
    run_id: str,
    usage: Mapping[str, Any] | None,
    node_records: Sequence[Mapping[str, Any]],
    launched: Mapping[str, Any] | None,
    host: str | None = None,
    provider_profile: str | None = None,
    provider_profile_sha: str | None = None,
) -> dict[str, Any]:
    """One `runs` row from a run's usage summary, its node records and its launch
    marker. `host` names the ingesting machine, not necessarily the one the run
    executed on — a backfilled, inferred value, not an observed one — and
    `provider_profile` comes from the autonomy ledger or, failing that, the run's own
    node records; both are the edge's job to resolve and pass in, never read here.
    `provider_profile_sha` is likewise resolved by the edge, either from a direct
    record or, for a run predating one, from `coxswain-cartridges` git history
    (`resolve_provider_profile_sha`); None here means unresolved, not "not needed"."""
    summary = (usage or {}).get("summary") or {}
    first_node = node_records[0] if node_records else {}
    minutes = sum(float(n.get("human_minutes") or 0.0) for n in node_records)
    return {
        "run_id": run_id,
        "started_at": summary.get("started_at"),
        "ended_at": summary.get("ended_at"),
        "cartridge_sha": first_node.get("cartridge_sha"),
        "cartridge_team": first_node.get("cartridge_team"),
        "provider_profile": provider_profile,
        "provider_profile_sha": provider_profile_sha,
        "vendor": "claude-code",
        "host": host,
        "launched_by": (launched or {}).get("launched_by"),
        "principal": first_node.get("principal"),
        "human_minutes": minutes or None,
        "schema_version": SCHEMA_VERSION,
    }


def call_rows(run_id: str, usage: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """One `calls` row per model invocation in a run's usage record, in call order.
    `task_id`/`join_confidence`/`failure_class` are left unset here: assigning them
    needs this run's task records and its calls' own trace files, which only the
    edge (`ingest`, via `assign_task_ids`/`fill_failure_classes`) has. `challenger`
    is true exactly when the call's own `reason` is the literal string "challenger" —
    router-steward.md §2, verbatim: "challenger status travels entirely in `reason`:
    `select_tier` returns the fixed literal string `\"challenger\"`, not a prefix.
    The CLI edge matches that exact string to set the `challenger` column ... on the
    call row." `select_tier` itself is a later ticket's pure function and writes no
    code in this repo yet (no hit for `select_tier` outside docs/design), so `reason`
    is read here ahead of that CLI edge; a call with no `reason`, or any other
    reason, ingests as `challenger=0`, never NULL (charter B4)."""
    calls = list((usage or {}).get("calls") or [])
    attempts = attempt_numbers(calls)
    return [
        {
            "run_id": run_id,
            "seq": seq,
            "role": call.get("role"),
            "attempt": attempt,
            "tier": call.get("tier"),
            "model": call.get("model"),
            "model_id": call.get("model_id"),
            "cost_usd": call.get("cost_usd"),
            "turns": call.get("turns"),
            "duration_ms": call.get("duration_ms"),
            "input_tokens": call.get("input_tokens"),
            "cache_read_tokens": call.get("cache_read_tokens"),
            "cache_creation_tokens": call.get("cache_creation_tokens"),
            "output_tokens": call.get("output_tokens"),
            "tools": json.dumps(call["tools"]) if call.get("tools") is not None else None,
            "trace_path": call.get("trace"),
            "failure_class": None,
            "challenger": int(call.get("reason") == "challenger"),
            "task_id": None,
            "join_confidence": None,
            "recovered_from_trace": 0,
        }
        for seq, (call, attempt) in enumerate(zip(calls, attempts))
    ]


def recovered_call_rows(run_id: str, traces: Sequence[tuple[str, Sequence[Mapping[str, Any]]]]) -> list[dict[str, Any]]:
    """One `calls` row per trace file, for a run whose usage record is absent (spec §1
    fact 5: a budget-stopped run writes no `usage.json` at all, so its cost survives
    only in each trace's terminal `type: result` line). `traces` is `(trace_path,
    parsed_events)` pairs already in `_read_traces`' NODE_ORDER order (role, then
    attempt index) so `seq` reflects execution order across roles, the same guarantee
    `_node_sort_key` gives `<run>:<node>.json` records; `role` comes from the
    `<role>-<n>.jsonl` stem, and `_final_result` — the same parser `extract_failure_class`
    already uses — is the only place `total_cost_usd` and `num_turns` are read from.
    Every row here carries `recovered_from_trace=1`, so a query can exclude or flag it
    the way `join_confidence` already flags a heuristic task join. Token counts,
    `duration_ms`, `tier`, `model` and `model_id` are unrecoverable from the result line and stay
    unset."""
    roles = [Path(path).stem.rsplit("-", 1)[0] for path, _ in traces]
    attempts = attempt_numbers([{"role": role} for role in roles])
    return [
        {
            "run_id": run_id,
            "seq": seq,
            "role": role,
            "attempt": attempt,
            "tier": None,
            "model": None,
            "model_id": None,
            "cost_usd": _final_result(events).get("total_cost_usd"),
            "turns": _final_result(events).get("num_turns"),
            "duration_ms": None,
            "input_tokens": None,
            "cache_read_tokens": None,
            "cache_creation_tokens": None,
            "output_tokens": None,
            "tools": None,
            "trace_path": path,
            "failure_class": extract_failure_class(events, "", role),
            "challenger": 0,
            "task_id": None,
            "join_confidence": None,
            "recovered_from_trace": 1,
        }
        for seq, (role, attempt, (path, events)) in enumerate(zip(roles, attempts, traces))
    ]


def _rounds(value: Any) -> int | None:
    """A list's length, a dict's own 'rounds' key, or None when neither shape is present."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        return value.get("rounds")
    return None


def _fix_loop_attempts(record: Mapping[str, Any]) -> int:
    """A task record's build-attempt count for the join identity (stats-join-spike.md):
    `fix_loop`'s own 'attempts' field when `fix_loop` is a dict, its length when
    `fix_loop` is a list of rounds, or 1 when `fix_loop` is absent — a task that never
    entered the fix loop still made its one build call."""
    fix_loop = record.get("fix_loop")
    if isinstance(fix_loop, Mapping):
        attempts = fix_loop.get("attempts")
        return attempts if isinstance(attempts, int) else 1
    if isinstance(fix_loop, list):
        return len(fix_loop) or 1
    return 1


def run_join_holds(attempts: Sequence[int], build_call_count: int) -> bool:
    """True when this run's counting identity holds: `sum(attempts)` — one entry
    per task record, each from `_fix_loop_attempts` — equals its build call count.
    The spike verifies this in 171/184 runs; a run where it fails gets no task_id."""
    return sum(attempts) == build_call_count


def assign_task_ids(
    calls: Sequence[Mapping[str, Any]], task_ids: Sequence[str], attempts: Sequence[int]
) -> list[dict[str, Any]]:
    """Sets `task_id`/`join_confidence` on every one of this run's calls. When
    `run_join_holds`, each build call gets the ticket at its position in
    `task_ids` repeated `attempts` times (ticket order x attempts) and
    `join_confidence='heuristic'`; every call otherwise — every call in this run
    when the identity fails, and every non-build call when it holds — gets no
    `task_id` and `join_confidence='none'`, written per row so an aggregate can
    always exclude or flag it."""
    build_seq = [i for i, c in enumerate(calls) if c.get("role") == "build"]
    ordered_task_ids = [task_id for task_id, n in zip(task_ids, attempts) for _ in range(n)]
    assigned = dict(zip(build_seq, ordered_task_ids)) if run_join_holds(attempts, len(build_seq)) else {}
    return [
        {**call, "task_id": assigned.get(i), "join_confidence": "heuristic" if i in assigned else "none"}
        for i, call in enumerate(calls)
    ]


def call_trace_key(call: Mapping[str, Any]) -> str | None:
    """The key a usage call's trace is filed under: its own `trace` string, or its `id`
    when it has none (a run the trace store answers for carries no loose trace path)."""
    return call.get("trace") or call.get("id") or None


def fill_failure_classes(
    calls: Sequence[Mapping[str, Any]],
    traces_by_path: Mapping[str, Sequence[Mapping[str, Any]]],
    log_excerpt: str = "",
    keys: Sequence[str | None] | None = None,
) -> list[dict[str, Any]]:
    """Sets `failure_class` on every call whose `failure_class` is still unset and
    whose key names a trace in `traces_by_path`, via the same `extract_failure_class`
    `recovered_call_rows` already uses — 'ok' for a successful call, never unset where
    the trace has a result line. The key is the call's `trace_path` unless `keys`
    gives one per call (`call_trace_key` of each usage call, so a store call with no
    trace path still finds its trace by id). A call with no trace, or one
    `recovered_call_rows` already classified, passes through."""
    return [
        {**call, "failure_class": extract_failure_class(traces_by_path[key], log_excerpt, call.get("role"))}
        if call.get("failure_class") is None and key in traces_by_path
        else dict(call)
        for call, key in zip(calls, keys if keys is not None else [c.get("trace_path") for c in calls])
    ]


def _work_store_ticket_done(work_store_root: Path, ticket: Any) -> bool:
    """True when some `work_store_root/*/*/ticket.md` (work-item ids are unique
    across the store, so the first match wins) has frontmatter (parsed the way
    `route.work_item` reads a work item's own `state` field) carrying
    `state: done`. Never derives the initiative from the run name or the
    record: older runs are named `<initiative>-epic-N`/`<initiative>-decompose-N`,
    newer ones `<initiative>-N`, so no single strip is safe, and the record
    carries no top-level `initiative` in the corpus. False when `ticket` is
    missing or no file matches — never raises on a work store that doesn't
    cover this task."""
    if not ticket:
        return False
    matches = sorted(work_store_root.glob(f"*/*/{ticket}.md"))
    if not matches:
        return False
    fields, _ = parse_frontmatter(matches[0].read_text(encoding="utf-8"))
    return fields.get("state") == "done"


def _section_verdict(record: Mapping[str, Any], section: str) -> str | None:
    value = record.get(section)
    verdict = value.get("verdict") if isinstance(value, Mapping) else None
    return verdict if isinstance(verdict, str) else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _handoff_verdict(handoff: Any) -> str | None:
    """The bool `complete` as the yes/no labels `stats_examples.handoff_example` uses; no repo code writes a handoff `verdict`."""
    complete = handoff.get("complete") if isinstance(handoff, Mapping) else None
    if isinstance(complete, bool):
        return "yes" if complete else "no"
    return None


def _plan_gate_verdict(plan_gate: Any) -> str | None:
    """A dict's `verdict`, or the bare string a gate section can be written as."""
    if isinstance(plan_gate, Mapping):
        return _text_or_none(plan_gate.get("verdict"))
    return plan_gate if isinstance(plan_gate, str) and plan_gate else None


def _arbiter_facts(arbitration: Any) -> tuple[str | None, str | None, str | None]:
    """(verdict, sided_with, state): "ruled" only when the dict states a verdict, "skipped" for the arbiter-skip string."""
    if isinstance(arbitration, Mapping):
        verdict = _text_or_none(arbitration.get("verdict"))
        return verdict, _text_or_none(arbitration.get("sided_with")), "ruled" if verdict else None
    if arbitration == _ARBITER_SKIPPED:
        return None, None, "skipped"
    return None, None, None


def _stopped_flag(stopped: Any) -> int | None:
    """1 for a stop reason ("budget", "attempts_exhausted") or True, 0 for an explicit null or False, else None."""
    if isinstance(stopped, bool):
        return int(stopped)
    if isinstance(stopped, str) and stopped:
        return 1
    if stopped is None:
        return 0
    return None


def _fix_loop_facts(record: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """(attempts, stopped). Attempts is `_fix_loop_attempts`'s own count where the record states one, so the column
    agrees with the join identity, and None where that function would fall back to its default of 1.
    Stopped is None unless a `fix_loop` dict carries a `stopped` key."""
    fix_loop = record.get("fix_loop")
    stated = (isinstance(fix_loop, Mapping) and isinstance(fix_loop.get("attempts"), int)) or (
        isinstance(fix_loop, list) and bool(fix_loop)
    )
    keyed = isinstance(fix_loop, Mapping) and "stopped" in fix_loop
    return (
        _fix_loop_attempts(record) if stated else None,
        _stopped_flag(fix_loop["stopped"]) if keyed else None,
    )


def gate_facts(record: Mapping[str, Any]) -> dict[str, Any]:
    """The nine gate columns of one task record; a fact the record does not state is None, never 0 or ""."""
    arbiter_verdict, sided_with, arbiter_state = _arbiter_facts(record.get("arbitration"))
    attempts, stopped = _fix_loop_facts(record)
    return {
        "handoff_verdict": _handoff_verdict(record.get("handoff")),
        "charter_verdict": _section_verdict(record, "review"),
        # `adversary` as {"verdict": ...} is what land._verdict reads; a findings list carries none and reads None.
        "adversary_verdict": _section_verdict(record, "adversary"),
        "arbiter_verdict": arbiter_verdict,
        "arbiter_sided_with": sided_with,
        "arbiter_state": arbiter_state,
        "fix_loop_attempts": attempts,
        "fix_loop_stopped": stopped,
        "plan_gate_verdict": _plan_gate_verdict(record.get("plan_gate")),
    }


def task_row(
    run_id: str,
    phase: str,
    ticket: str,
    record: Mapping[str, Any],
    gate_diffs: Sequence[Mapping[str, Any]],
    log_events: Sequence[Event],
    work_store_root: Path | None = None,
) -> dict[str, Any]:
    """One `tasks` row from one task record, with `gate_diffs` (gathered from the
    run's node records) and `log_events` (parsed from the run's log) folded in so
    `resolve_outcome` sees the full picture and never guesses from a run-level
    budget stop, which names no ticket. `work_store_root`, when given, is checked
    for this ticket's `state: done` ahead of `gate_diffs` and the log line (spec:
    the work store outranks both but never the record's own explicit `landed`
    field)."""
    scoped_ticket = record.get("ticket", ticket)
    work_store_done = work_store_root is not None and _work_store_ticket_done(work_store_root, scoped_ticket)
    scoped = {
        **record,
        "ticket": scoped_ticket,
        "gate_diffs": gate_diffs,
        "log_events": log_events,
        "work_store_done": work_store_done,
    }
    outcome, outcome_source = resolve_outcome(scoped)
    attempt = record.get("attempt")
    attempts = record.get("attempts")
    outcome_kind = (
        attempts[-1].get("kind")
        if isinstance(attempts, Sequence) and attempts and isinstance(attempts[-1], Mapping)
        else record.get("kind")
    )
    return {
        "run_id": run_id,
        "task_id": record.get("run_id") or f"{run_id}:{phase}:{ticket}",
        "ticket": scoped_ticket,
        "phase": phase,
        "initiative": record.get("initiative"),
        "attempt": attempt if isinstance(attempt, int) else None,
        "outcome": outcome,
        "outcome_source": outcome_source,
        "review_rounds": _rounds(record.get("review")),
        "arbitration_verdict": arbitration_verdict(record),
        "fix_loop_rounds": _rounds(record.get("fix_loop")),
        "cost_usd": record.get("cost_usd"),
        "reason": record.get("reason"),
        "outcome_kind": outcome_kind,
        **gate_facts(record),
    }


def rollup_task_costs(calls: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each task with `cost_usd` replaced by the sum of `cost_usd` over calls whose
    `task_id` matches and whose `join_confidence` isn't 'none'; `None` (never 0.0)
    for a task no joined call names, or where every joined call's own cost is unset."""
    joined_by_task: dict[Any, list[Any]] = {}
    for call in calls:
        if call.get("join_confidence") == "none" or call.get("task_id") is None:
            continue
        joined_by_task.setdefault(call["task_id"], []).append(call.get("cost_usd"))
    result = []
    for task in tasks:
        costs = [c for c in joined_by_task.get(task["task_id"], []) if c is not None]
        result.append({**task, "cost_usd": sum(costs) if costs else None})
    return result


@dataclass(frozen=True)
class IngestReport:
    runs_ingested: int
    unparsed_count: int
    unparsed_sample: tuple[str, ...]
    provider_profile_from_ledger: int = 0
    provider_profile_from_node: int = 0
    provider_profile_unresolved: int = 0
    provider_profile_sha_resolved: int = 0
    provider_profile_sha_unresolved: int = 0
    challenger_calls: int = 0


def discover_runs(runs_dir: Path) -> list[str]:
    """Run ids present under `runs_dir`, from `.usage.json`, `.launched.json`,
    `tasks/` stems, `<run>:<node>.json` records and `<run>-trace/` directories — a run
    that only ever wrote node records (no usage.json, no launched.json, no tasks/) is
    still a run, and so is one whose only surviving artifact is its trace directory
    (spec §1 fact 5: a budget-stopped run can write no usage.json at all). An ended
    run that only the run store knows (usage rows or phase manifests) is a run too."""
    usage_ids = {p.name[: -len(".usage.json")] for p in runs_dir.glob("*.usage.json")}
    launched_ids = {p.name[: -len(".launched.json")] for p in runs_dir.glob("*.launched.json")}
    task_ids = {p.name for p in runs_dir.glob("*") if p.is_dir() and (p / "tasks").is_dir()}
    node_ids = {p.name.split(":", 1)[0] for p in runs_dir.glob("*:*.json")}
    trace_ids = {p.name.removesuffix("-trace") for p in runs_dir.glob("*-trace") if p.is_dir()}
    store_ids = set(run_store.usages(runs_dir)) | set(run_store.all_phase_manifests(runs_dir))
    return sorted(usage_ids | launched_ids | task_ids | node_ids | trace_ids | store_ids)


_NODE_INDEX = {name: i for i, name in enumerate(NODE_ORDER)}


def _manifest_sort_key(manifest: Mapping[str, Any]) -> tuple[int, str]:
    """A store manifest's position in NODE_ORDER, from its `run_id` (`<run>:<node>`) —
    the same key `_node_sort_key` reads off a file name."""
    node = str(manifest.get("run_id", "")).split(":", 1)[-1]
    return (_NODE_INDEX.get(node, len(NODE_ORDER)), node)


def _node_sort_key(path: Path) -> tuple[int, str]:
    """A `<run>:<node>.json` path's position in the graph's own NODE_ORDER, so
    gate_diffs concatenate in execution order rather than filename alphabetical
    order; a node NODE_ORDER doesn't name sorts after every named one."""
    node = path.name.split(":", 1)[-1].removesuffix(".json")
    return (_NODE_INDEX.get(node, len(NODE_ORDER)), node)


def _read_json(path: Path, unparsed: list[str]) -> dict[str, Any] | None:
    """The parsed JSON object at `path`, or None with `path` appended to `unparsed`
    when the file is absent, is not valid JSON, is not readable as UTF-8, or fails
    to read at all, or parses to something other than an object."""
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        unparsed.append(str(path))
        return None
    if not isinstance(parsed, dict):
        unparsed.append(str(path))
        return None
    return parsed


def _read_log_lines(path: Path, unparsed: list[str]) -> list[str]:
    """A run's log lines, or `[]` with `path` appended to `unparsed` when the file
    is absent, is not readable as UTF-8, or fails to read at all."""
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        unparsed.append(str(path))
        return []


def _read_traces(trace_dir: Path, unparsed: list[str]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Every `<role>-<n>.jsonl` trace file under `trace_dir`, ordered by the graph's own
    NODE_ORDER (then by `n` within a role) — the same rule `_node_sort_key` already
    applies to `<run>:<node>.json` records, so a recovered run's calls come out in
    execution order rather than filename alphabetical order (`build` sorting before
    `decompose` would otherwise misorder `calls.seq`). A role NODE_ORDER doesn't name
    sorts after every named one. A name whose stem does not split into a role and a
    trailing integer, or a file that fails to read, is appended to `unparsed` instead
    of raising."""
    if not trace_dir.exists():
        return []
    keyed: list[tuple[tuple[int, str, int], Path]] = []
    for path in trace_dir.glob("*.jsonl"):
        role, _, idx = path.stem.rpartition("-")
        if not role or not idx.isdigit():
            unparsed.append(str(path))
            continue
        keyed.append(((_NODE_INDEX.get(role, len(NODE_ORDER)), role, int(idx)), path))
    pairs: list[tuple[str, list[dict[str, Any]]]] = []
    for _, path in sorted(keyed, key=lambda kv: kv[0]):
        try:
            pairs.append((str(path), load_trace(path)))
        except (OSError, UnicodeDecodeError):
            unparsed.append(str(path))
    return pairs


def _resolve_trace_path(runs_dir: Path, trace: str) -> Path:
    """A call's own `trace` string, resolved against `runs_dir` when it is not
    already absolute — the same base `_read_traces` builds `<run>-trace/` under."""
    path = Path(trace)
    return path if path.is_absolute() else runs_dir / path


def _read_call_traces(
    runs_dir: Path, run_id: str, usage: Mapping[str, Any] | None, unparsed: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every distinct trace a run's usage calls name, keyed by `call_trace_key` (the
    call's `trace` string, else its `id`), so `ingest` can hand each call's key to
    `fill_failure_classes`. A loose trace file that exists is read as before. A call
    whose file is gone, or that names none, reads the trace store, one
    `run_store.call_events` per call because it has no batch form. A call found in
    neither is left out (storing `[]` would class it 'unknown'), and its named path
    is recorded in `unparsed` as before. A store that cannot be read is recorded once
    for the run, and the remaining calls skip it."""
    traces: dict[str, list[dict[str, Any]]] = {}
    broken: list[str] = []
    for call in (usage or {}).get("calls") or []:
        key = call_trace_key(call)
        if not key or key in traces:
            continue
        path = _resolve_trace_path(runs_dir, call["trace"]) if call.get("trace") else None
        if path is not None and path.exists():
            try:
                traces[key] = load_trace(path)
            except (OSError, UnicodeDecodeError):
                unparsed.append(str(path))
        elif events := _call_store_events(runs_dir, run_id, call, broken):
            traces[key] = events
        elif path is not None:
            unparsed.append(str(path))
    unparsed.extend(broken)
    return traces


def _store_errors() -> tuple[type[Exception], ...]:
    """What reading the trace store can raise: `TracesUnavailable`, `OSError` from a day
    file, `ValueError` from a decode, `KeyError` from a row without `seq`, and
    `zstandard.ZstdError` (not an `OSError`) from a truncated frame when it is installed."""
    base: tuple[type[Exception], ...] = (run_store.TracesUnavailable, OSError, ValueError, KeyError)
    try:
        import zstandard
    except ImportError:
        return base
    zstd_error = getattr(zstandard, "ZstdError", None)
    return base if zstd_error is None else (*base, zstd_error)


def _call_store_events(runs_dir: Path, run_id: str, call: Mapping[str, Any], broken: list[str]) -> list[dict[str, Any]]:
    """The call's events from the trace store, `[]` when it has none. The first store
    error is appended to `broken`, and later calls skip the store: each would re-read
    the same bad day file and repeat the same entry."""
    if broken:
        return []
    try:
        return run_store.call_events(runs_dir, run_id, call) or []
    except _store_errors() as exc:
        broken.append(f"{runs_dir / run_store.TRACES_DIRNAME}: {type(exc).__name__}")
        return []


def _read_ledger(path: Path, unparsed: list[str]) -> list[dict[str, Any]]:
    """Every JSON object in the autonomy ledger, one per line. A missing file reads
    as no rows (most ingests run without one); a line that is not a JSON object is
    appended to `unparsed` instead of raising."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        unparsed.append(str(path))
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            unparsed.append(f"{path}#{line[:40]}")
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _read_calls_jsonl(path: Path, unparsed: list[str]) -> list[dict[str, Any]] | None:
    """Every call dict in `<run>.calls.jsonl`, one per line (observed-record.md §1-3:
    a usage.json call dict plus `ts`/`ok`/`error`), each tagged `source: calls_jsonl`
    so downstream code can tell it apart from a usage.json or trace-recovered call.
    `None` when `path` does not exist, distinct from `[]` for a file present but
    empty; a line that is not a JSON object is appended to `unparsed` instead of
    raising, the same rule `_read_ledger` applies to the autonomy ledger."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        unparsed.append(str(path))
        return None
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            unparsed.append(f"{path}#{line[:40]}")
            continue
        if isinstance(parsed, dict):
            rows.append({**parsed, "source": "calls_jsonl"})
        else:
            unparsed.append(f"{path}#{line[:40]}")
    return rows


def _ledger_run_id(row: Mapping[str, Any]) -> str | None:
    """The run_id half of a ledger row's '<run>:<node>' key (stats-join-spike.md:
    'keys rows on <run>:<node>'), or the row's own `run_id`/`run` field when it
    carries one directly instead of a composite key."""
    key = row.get("key")
    if isinstance(key, str) and ":" in key:
        return key.split(":", 1)[0]
    run_id = row.get("run_id") or row.get("run")
    return run_id if isinstance(run_id, str) else None


def provider_profile_for(run_id: str, ledger_rows: Sequence[Mapping[str, Any]]) -> str | None:
    """This run's `provider_profile`: the first ledger row whose key names it, since
    the ledger carries the field on every row (stats-join-spike.md) while the store
    has it nowhere else. None when no row names this run."""
    for row in ledger_rows:
        if _ledger_run_id(row) == run_id:
            profile = row.get("provider_profile")
            if profile:
                return profile
    return None


def provider_profile_from_nodes(node_records: Sequence[Mapping[str, Any]]) -> str | None:
    """This run's `provider_profile` from its own `<run>:<node>.json` records —
    the same key the ledger uses (chair evidence 2026-09-08: 200/200 node records
    carry it). None when no node record names it, the case `provider_profile_source`
    falls back from the ledger into."""
    for node in node_records:
        profile = node.get("provider_profile")
        if profile:
            return profile
    return None


def provider_profile_source(
    run_id: str, ledger_rows: Sequence[Mapping[str, Any]], node_records: Sequence[Mapping[str, Any]]
) -> tuple[str | None, str]:
    """This run's resolved `provider_profile` and which source supplied it: 'ledger'
    when an autonomy-ledger row names it, 'node' when only a `<run>:<node>.json`
    record does, 'none' when neither does. The ledger wins on a conflict, since it is
    the source `provider_profile_for` already trusted."""
    ledger_profile = provider_profile_for(run_id, ledger_rows)
    if ledger_profile:
        return ledger_profile, "ledger"
    node_profile = provider_profile_from_nodes(node_records)
    if node_profile:
        return node_profile, "node"
    return None, "none"


def resolve_provider_profile_sha(
    cartridges_repo: Path | None, profile: str | None, started_at: str | None
) -> str | None:
    """The git blob sha of `providers/<profile>.yaml` in `cartridges_repo` as it
    stood at `started_at`: the file's content at the last commit at or before that
    timestamp to touch it, read via `git log --before` then `git rev-parse
    <commit>:<path>`. None when `cartridges_repo`, `profile` or `started_at` is
    missing, `cartridges_repo` is not a git checkout, no commit at or before
    `started_at` touched the file, or the file did not exist at that commit — never
    guessed, only ever resolved or left absent (charter B4: absence is reported,
    not hidden behind a default)."""
    if cartridges_repo is None or not profile or not started_at:
        return None
    rel_path = f"providers/{profile}.yaml"
    try:
        log = subprocess.run(
            ["git", "-C", str(cartridges_repo), "log", f"--before={started_at}", "-1", "--format=%H", "--", rel_path],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    commit = log.stdout.strip()
    if not commit:
        return None
    try:
        blob = subprocess.run(
            ["git", "-C", str(cartridges_repo), "rev-parse", f"{commit}:{rel_path}"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return blob.stdout.strip() or None


def load_run(
    runs_dir: Path, run_id: str, manifests: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
) -> dict[str, Any]:
    """Every file this ingester reads for one run — usage, launch marker, node
    records, task records and log lines — plus the paths that failed to parse.
    When `usage` is absent, `<run>.calls.jsonl` is read next (observed-record.md
    §4: the second call source, after usage.json and before trace recovery); its
    calls are returned tagged `source: calls_jsonl` under the `calls_jsonl` key.
    Trace files are read only when both `usage` and `calls_jsonl` are absent: a
    usage record's `calls[]` is authoritative, and `.usage.json` is exactly the
    file fact 5 says a budget-stopped run never wrote. A run whose usage.json IS
    present but undercounts a call (a different, already-measured symptom) is out
    of scope here and reads unrecovered, exactly as before this ticket. A run with no
    `<run>:*.json` file takes its node records from the run store; `manifests` is
    `run_store.all_phase_manifests(runs_dir)` when the caller already has it, because
    `run_store.phase_manifests` re-scans every manifest file and the whole `phases`
    table on each call, which over a corpus of store runs is quadratic."""
    unparsed: list[str] = []
    usage_path = runs_dir / f"{run_id}.usage.json"
    usage = _read_json(usage_path, unparsed) if usage_path.exists() else run_store.usage(runs_dir, run_id)
    launched = _read_json(runs_dir / f"{run_id}.launched.json", unparsed)
    calls_jsonl = _read_calls_jsonl(runs_dir / f"{run_id}.calls.jsonl", unparsed) if usage is None else None
    traces = (
        _read_traces(runs_dir / f"{run_id}-trace", unparsed) if usage is None and calls_jsonl is None else []
    )
    call_traces = _read_call_traces(runs_dir, run_id, usage, unparsed) if usage is not None else {}
    node_paths = sorted(runs_dir.glob(f"{run_id}:*.json"), key=_node_sort_key)
    node_records = (
        [
            parsed
            for path in node_paths
            for parsed in [_read_json(path, unparsed)]
            if parsed is not None
        ]
        if node_paths
        else sorted(
            manifests.get(run_id, []) if manifests is not None else run_store.phase_manifests(runs_dir, run_id),
            key=_manifest_sort_key,
        )
    )
    tasks_root = runs_dir / run_id / "tasks"
    task_files = [
        (path.parent.name, path.stem, parsed)
        for path in sorted(tasks_root.glob("*/*.json"))
        for parsed in [_read_json(path, unparsed)]
        if parsed is not None
    ]
    log_lines = _read_log_lines(runs_dir / f"{run_id}.log", unparsed)
    return {
        "usage": usage,
        "launched": launched,
        "node_records": node_records,
        "task_files": task_files,
        "log_lines": log_lines,
        "traces": traces,
        "call_traces": call_traces,
        "calls_jsonl": calls_jsonl or [],
        "unparsed": unparsed,
    }


def _upsert(
    conn: sqlite3.Connection,
    run_id: str,
    run: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
) -> None:
    """Replaces this run_id's rows in all three tables, so ingesting the same directory twice is a no-op."""
    run_cols = ", ".join(c.name for c in RUNS_COLUMNS)
    run_placeholders = ", ".join(f":{c.name}" for c in RUNS_COLUMNS)
    conn.execute(f"INSERT OR REPLACE INTO runs ({run_cols}) VALUES ({run_placeholders})", run)

    conn.execute("DELETE FROM calls WHERE run_id = ?", (run_id,))
    call_cols = ", ".join(c.name for c in CALLS_COLUMNS)
    call_placeholders = ", ".join(f":{c.name}" for c in CALLS_COLUMNS)
    if calls:
        conn.executemany(f"INSERT INTO calls ({call_cols}) VALUES ({call_placeholders})", calls)

    conn.execute("DELETE FROM tasks WHERE run_id = ?", (run_id,))
    task_cols = ", ".join(c.name for c in TASKS_COLUMNS)
    task_placeholders = ", ".join(f":{c.name}" for c in TASKS_COLUMNS)
    if tasks:
        conn.executemany(f"INSERT INTO tasks ({task_cols}) VALUES ({task_placeholders})", tasks)


def ingest(
    runs_dir: Path | str,
    db_path: Path | str,
    ledger_path: Path | str | None = None,
    work_store_root: Path | str | None = None,
    cartridges_repo: Path | str | None = None,
) -> IngestReport:
    """Upserts every run under `runs_dir` into the stats store at `db_path`, keyed
    by run_id so re-running over the same directory changes nothing. Errors before
    any write when `runs_dir` does not exist, or exists but holds no run records —
    an empty ingest is not a successful one (charter B4). `ledger_path` defaults to
    the autonomy ledger's own path so `provider_profile_source` has rows to read;
    tests pass their own fixture instead. `work_store_root`, like `runs_dir`, is a
    runtime path argument resolved here, never a build-time default: `None` (the
    default) means no task in this ingest can resolve `outcome_source='work_store'`.
    Every run's `provider_profile` is tallied by which source resolved it (ledger,
    node record, or neither); the three counts in the returned report always sum to
    `runs_ingested`. `cartridges_repo`, when given, is a local `coxswain-cartridges`
    checkout `resolve_provider_profile_sha` reads to backfill each run's
    `provider_profile_sha` from git history; `None` (the default) means every run's
    sha is unresolved, reported as such rather than silently omitted. The two sha
    counts in the returned report always sum to `runs_ingested`. `challenger_calls`
    in the returned report is the total count of calls, across every run ingested,
    tagged `challenger=1`."""
    runs_dir = Path(runs_dir)
    work_store_root = Path(work_store_root) if work_store_root is not None else None
    cartridges_repo = Path(cartridges_repo) if cartridges_repo is not None else None
    if not runs_dir.is_dir():
        raise FileNotFoundError(f"no such runs directory: {runs_dir.resolve()}")
    run_ids = discover_runs(runs_dir)
    if not run_ids:
        raise FileNotFoundError(f"no run records found under: {runs_dir.resolve()}")

    conn = connect(db_path)
    unparsed: list[str] = []
    ledger_rows = _read_ledger(Path(ledger_path) if ledger_path is not None else LEDGER_PATH, unparsed)
    host = socket.gethostname()
    # profile_sources mirrors `unparsed`'s own accumulation just above: ingest is the
    # edge, already imperative and already writing the database per run (A7), so one
    # more per-run list append here costs nothing a pure core would have avoided.
    profile_sources: list[str] = []
    sha_resolutions: list[bool] = []
    challenger_calls = 0
    manifests = run_store.all_phase_manifests(runs_dir)
    for run_id in run_ids:
        loaded = load_run(runs_dir, run_id, manifests)
        unparsed.extend(loaded["unparsed"])
        gate_diffs = [d for node in loaded["node_records"] for d in (node.get("gate_diffs") or [])]
        log_events = from_log(run_id, loaded["log_lines"])
        profile, profile_source = provider_profile_source(run_id, ledger_rows, loaded["node_records"])
        profile_sources.append(profile_source)
        started_at = ((loaded["usage"] or {}).get("summary") or {}).get("started_at")
        profile_sha = resolve_provider_profile_sha(cartridges_repo, profile, started_at)
        sha_resolutions.append(profile_sha is not None)
        run = run_row(
            run_id, loaded["usage"], loaded["node_records"], loaded["launched"],
            host=host, provider_profile=profile, provider_profile_sha=profile_sha,
        )
        calls = (
            recovered_call_rows(run_id, loaded["traces"])
            if loaded["usage"] is None and loaded["traces"]
            # "" mirrors recovered_call_rows: extract_failure_class's log_excerpt must be
            # scoped to one call, and load_run has no per-call log slice, only the whole
            # run's — passing that would misclassify every call in a run by any other
            # call's log line (e.g. one call's budget_stop bleeding onto another's).
            else fill_failure_classes(
                call_rows(run_id, loaded["usage"]),
                loaded["call_traces"],
                "",
                [call_trace_key(c) for c in (loaded["usage"] or {}).get("calls") or []],
            )
        )
        tasks = [
            task_row(run_id, phase, ticket, record, gate_diffs, log_events, work_store_root)
            for phase, ticket, record in loaded["task_files"]
        ]
        attempts = [_fix_loop_attempts(record) for _, _, record in loaded["task_files"]]
        joined_calls = assign_task_ids(calls, [t["task_id"] for t in tasks], attempts)
        tasks = rollup_task_costs(joined_calls, tasks)
        challenger_calls += sum(c.get("challenger") or 0 for c in joined_calls)
        _upsert(conn, run_id, run, joined_calls, tasks)
    conn.commit()
    conn.close()
    assert len(profile_sources) == len(run_ids)
    assert len(sha_resolutions) == len(run_ids)
    return IngestReport(
        runs_ingested=len(run_ids), unparsed_count=len(unparsed), unparsed_sample=tuple(unparsed[:5]),
        provider_profile_from_ledger=profile_sources.count("ledger"),
        provider_profile_from_node=profile_sources.count("node"),
        provider_profile_unresolved=profile_sources.count("none"),
        provider_profile_sha_resolved=sum(sha_resolutions),
        provider_profile_sha_unresolved=len(sha_resolutions) - sum(sha_resolutions),
        challenger_calls=challenger_calls,
    )
