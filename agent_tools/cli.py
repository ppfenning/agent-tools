"""cox — the coxswain's operator tools (alias: agent-tools, removed next release)."""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import datetime
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import types
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools import (
    chair,
    chair_cap,
    chair_exec,
    chair_facts,
    chair_read_approved,
    chair_read_attempts,
    chair_read_docket,
    chair_read_exits,
    chair_read_housekeeping,
    chair_read_intake,
    chair_read_lease,
    chair_read_live,
    chair_read_lost,
    chair_read_patch,
    chair_read_quarantined,
    chair_read_record,
    chair_read_remote_unfetched,
    chair_read_run_id,
    chair_read_stranded,
    chair_report,
    chair_run,
    chair_service,
    cleanup,
    commands,
    courier,
    doctor,
    draft_apply,
    draft_list,
    epic,
    forge,
    forge_github,
    generate,
    host_cmd,
    install,
    install_exec,
    lake_config,
    lake_lease,
    land,
    land_lease,
    land_repo_lease,
    lane_hosts,
    leader_chat,
    notify,
    pacing,
    plan,
    provenance,
    records,
    remote_doctor,
    remote_fetch,
    remote_lane,
    remote_launch,
    review_pr,
    route,
    route_drift,
    route_import,
    route_sync,
    route_sync_gh,
    router,
    run_store,
    runs_bar,
    runs_detail,
    runs_detail_screen,
    runs_stranded,
    runs_top,
    runs_top_screen,
    schema,
    setup_install,
    setup_screen,
    sources,
    stats_causes,
    stats_chair,
    stats_efficiency,
    stats_examples,
    stats_gates,
    stats_ingest,
    stats_lanes,
    stats_query,
    stats_schema,
    stats_system_one,
    stats_tiers_cmd,
    steward,
    steward_draft,
    store_cli,
    store_dialect,
    store_url,
    tracker,
    usage_window,
    work_state,
)
from agent_tools import runs as runs_module
from agent_tools.remote_argv import auth_status_argv, ssh_argv


def _runs_usage(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    header = None
    usage = run_store.usage(runs_dir, a.run_id)
    if usage is not None:
        s = records.usage_summary(usage)
    else:
        pid_text = _read_text_or_none(runs_dir / f"{a.run_id}.pid")
        pid = route.parse_pid(pid_text) if pid_text is not None else None
        live = epic.run_live(pid, runs_dir / f"{a.run_id}.pid")
        trace_dir = runs_dir / f"{a.run_id}-trace"
        traces = stats_ingest._read_traces(trace_dir, [])
        if not live and not traces:
            print(f"no usage record and no trace for {a.run_id} in {runs_dir}")
            return 2
        rows = stats_ingest.recovered_call_rows(a.run_id, traces)
        s = records.usage_summary({"run_id": a.run_id, "calls": rows})
        header = f"live (pid {pid}) — from the trace so far" if live else "not live — from the trace so far"
    if a.json:
        print(json.dumps({**s, "note": header} if header else s, indent=2)); return 0
    if header:
        print(header)
    print(f"{s['run_id']}: {s['calls']} calls, {s['turns']} turns, ${s['cost_usd']:.2f}, cache-read share {s['cache_read_share']}")
    print(records.format_table([{"role": k, **v} for k, v in s["by_role"].items()], ["role", "calls", "cost_usd", "turns"]))
    print(); print(records.format_table([{"model": k, **v} for k, v in s["by_model"].items()], ["model", "calls", "cost_usd", "turns"]))
    return 0


_SERIES_COLUMNS = ["run", "date", "cartridge_sha", "provider_profile", "calls", "turns", "cost_usd",
                   "cache_share", "tasks_landed", "quarantined", "review_rounds", "cost_per_landed",
                   "tier_ceiling", "effort_ceiling", "launched_by"]


def _runs_series(a: argparse.Namespace) -> int:
    d = Path(a.runs_dir)
    files = {f.name: f.read_text(encoding="utf-8")
             for pattern in ("*.usage.json", "*:*.json", "*.ceiling.json", "*.launched.json") for f in d.glob(pattern)}
    stored = {f"{run}.usage.json": json.dumps(usage) for run, usage in run_store.usages(d).items()}
    stored |= {f"{m['run_id']}.json": json.dumps(m)
               for ms in run_store.all_phase_manifests(d).values() for m in ms if m.get("run_id")}
    rows = records.series({**stored, **files})
    totals = records.series_totals(rows)
    if a.json:
        print(json.dumps({"rows": rows, "totals": totals}, indent=2))
    else:
        print(records.format_table(rows, _SERIES_COLUMNS))
        print(f"totals: {totals['runs']} runs, ${totals['cost_usd']:.2f}, {totals['tasks_landed']} landed, "
              f"{totals['quarantined']} quarantined, cost/landed {totals['cost_per_landed']}, "
              f"{totals['runs_landing_nothing']} landed nothing")
    if a.append:
        p = Path(a.append)
        new_lines = records.series_new_lines(p.read_text(encoding="utf-8") if p.exists() else "", rows)
        if new_lines:
            with p.open("a", encoding="utf-8") as fh:
                fh.write("".join(line + "\n" for line in new_lines))
    return 0


def _runs_events(a: argparse.Namespace) -> int:
    for ev in runs_module.events(Path(a.runs_dir), follow=a.follow):
        if a.json:
            print(json.dumps(dataclasses.asdict(ev)), flush=True)
        else:
            detail = " ".join(f"{k}={v}" for k, v in ev.detail.items())
            print(f"{ev.run} {ev.kind} {detail}".rstrip(), flush=True)
    return 0


def _stats_ingest(a: argparse.Namespace) -> int:
    try:
        report = stats_ingest.ingest(
            a.runs_dir, a.db, work_store_root=a.work_store_root, cartridges_repo=a.cartridges_repo,
        )
    except FileNotFoundError as exc:
        print(exc)
        return 1
    profile_summary = (
        f"provider_profile: {report.provider_profile_from_ledger} from ledger, "
        f"{report.provider_profile_from_node} from node record, "
        f"{report.provider_profile_unresolved} unresolved; "
        f"provider_profile_sha: {report.provider_profile_sha_resolved} resolved, "
        f"{report.provider_profile_sha_unresolved} unresolved"
    )
    if report.unparsed_count:
        print(f"{report.runs_ingested} run(s) ingested; {report.unparsed_count} file(s) failed to parse:")
        for path in report.unparsed_sample:
            print(f"  {path}")
        return 1
    print(f"{report.runs_ingested} run(s) ingested from {a.runs_dir} into {a.db} ({profile_summary})")
    return 0


def _stats_fetch(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _stats_roles(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.roles_report(
            _stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), _stats_fetch(conn, "runs"),
            cartridge_sha=a.cartridge_sha, provider_profile=a.provider_profile,
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _canonical_date(text: str) -> bool:
    """True only for YYYY-MM-DD. fromisoformat also takes `20260901` and `2026-W36-1`, which compare wrong as text in SQL."""
    try:
        return datetime.date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _stats_tiers(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats tiers: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    return stats_tiers_cmd.run(a.db, a.since, a.min_samples, a.json)


def _stats_causes(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats causes: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    summaries = stats_causes.summarise(run_store.attempt_causes(Path(a.runs_dir), a.since or ""))
    print(json.dumps(stats_causes.to_json(summaries), indent=2) if a.json else "\n".join(stats_causes.render_lines(summaries)))
    return 0


def _stats_efficiency(a: argparse.Namespace) -> int:
    if a.days < 1:
        print(f"cox stats efficiency: --days must be at least 1, got {a.days}", file=sys.stderr)
        return 2
    now = datetime.datetime.now(datetime.UTC)
    runs_dir = Path(a.runs_dir)
    since = (now - datetime.timedelta(days=a.days - 1)).date().isoformat()
    stored = run_store.efficiency_rows(runs_dir, since)
    log = (_read_text_or_none(runs_dir / "land.jsonl") or "").splitlines()
    lands = stats_efficiency.landed_lands(log)
    rows, total = stats_efficiency.efficiency(stored["calls"], stored["tasks"], lands, since, now, stats_efficiency.first_log_day(log))
    if a.json:
        print(json.dumps(stats_efficiency.to_json(rows, total), indent=2))
    else:
        print("\n".join(stats_efficiency.render_lines(rows, total)))
    return 0


def _print_gates(rows: list[dict], as_json: bool, min_sample: int) -> None:
    verdicts = [stats_gates.verdict_line(row, min_sample) for row in rows]
    print(json.dumps({"rows": rows, "verdicts": verdicts}, indent=2) if as_json else stats_query.render_gates(rows, verdicts))


def _stats_gates_from_store(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    rows = stats_gates.gate_rows(run_store.task_verdict_rows(runs_dir, a.since), run_store.gate_call_rows(runs_dir, a.since))
    if not rows:
        window = f" on or after {a.since}" if a.since is not None else ""
        print(f"no gate rows{window} in the run store under {a.runs_dir}", file=sys.stderr)
        return 1
    _print_gates(rows, a.json, a.min_sample)
    return 0


def _stats_gates(a: argparse.Namespace) -> int:
    if a.since is not None and not _canonical_date(a.since):
        print(f"cox stats gates: --since must be a date as YYYY-MM-DD, got {a.since!r}", file=sys.stderr)
        return 2
    if a.store:
        return _stats_gates_from_store(a)
    hint = f"no gate rows in {a.db}: run cox stats ingest"
    # stats_schema.connect creates a missing db, so a missing file is checked before the open.
    if not Path(a.db).exists():
        print(hint, file=sys.stderr)
        return 1
    conn = stats_schema.connect(a.db)
    try:
        rows = stats_gates.gate_rows(*stats_query.gates_inputs(conn, a.since))
        in_db = a.since is not None and not rows and bool(stats_gates.gate_rows(*stats_query.gates_inputs(conn, None)))
    finally:
        conn.close()
    if not rows:
        print(f"no gate rows on or after {a.since} in {a.db}: widen or drop --since" if in_db else hint, file=sys.stderr)
        return 1
    _print_gates(rows, a.json, a.min_sample)
    return 0


def _stats_coverage(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.coverage_report(
            _stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), _stats_fetch(conn, "runs"),
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _stats_explain(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.explain_report(_stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), a.role)
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped([report]))
    return 0


def _stats_series(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.series_report(
            _stats_fetch(conn, "runs"), _stats_fetch(conn, "tasks"),
            cartridge_sha=a.cartridge_sha, provider_profile=a.provider_profile,
        )
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _stats_spend_mix(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.spend_mix_report(_stats_fetch(conn, "calls"))
    finally:
        conn.close()
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _stats_lanes(a: argparse.Namespace) -> int:
    if a.hours < 1:
        print("--hours must be at least 1", file=sys.stderr)
        return 2
    now = datetime.datetime.now(datetime.UTC)
    spans = run_store.run_spans(Path(a.runs_dir), stats_lanes.window_start(now, a.hours).isoformat())
    rows = stats_lanes.lanes_by_hour(spans, now, a.hours)
    if a.json:
        print(json.dumps({"rows": rows, "totals": stats_lanes.totals(rows)}, indent=2))
    else:
        print(stats_lanes.render_table(rows, None))
        print(stats_lanes.render_totals(rows))
    return 0


def _file_date(p: Path) -> str:
    return datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.UTC).date().isoformat()


def _in_window(p: Path, since: str | None) -> bool:
    return since is None or _file_date(p) >= since


def _run_in_window(runs_dir: Path, run_id: str, since: str | None) -> bool:
    """A file run by its file's mtime date; a store-only run by the date of its `launched_at`, absent counting as outside."""
    p = runs_dir / f"{run_id}.usage.json"
    if p.exists():
        return _in_window(p, since)
    started = run_store.run_started(runs_dir, run_id)
    return since is None or (started is not None and started[:10] >= since)


def _json_or(text: str | None, default):
    try:
        return json.loads(text) if text is not None else default
    except ValueError:
        return default


def _chair_catalog_prices(a: argparse.Namespace) -> dict:
    """model -> price from `catalog.yaml` beside the profile's `provider_profile`, else `<cartridges_dir>/providers/catalog.yaml`, read as data; {} when the profile or both catalogs are unreadable."""
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    provider_profile, cartridges_dir = profile.get("provider_profile"), profile.get("cartridges_dir")
    candidates = [
        *([Path(provider_profile).expanduser().parent / "catalog.yaml"] if provider_profile else []),
        *([Path(cartridges_dir).expanduser() / "providers" / "catalog.yaml"] if cartridges_dir else []),
    ]
    catalog_text = next((t for p in candidates if (t := _read_text_or_none(p)) is not None), None)
    try:
        return stats_chair.catalog_prices(yaml.safe_load(catalog_text) if catalog_text is not None else None)
    except yaml.YAMLError:
        return {}


def _chair_transcript_lines(projects_dir: Path, session: str) -> list[str]:
    """Every line of the session transcript and of its subagent transcripts."""
    paths = [*projects_dir.glob(f"*/{session}.jsonl"), *projects_dir.glob(f"*/{session}/subagents/agent-*.jsonl")]
    return [line for p in sorted(paths) for line in (_read_text_or_none(p) or "").splitlines()]


def _stats_chair(a: argparse.Namespace) -> int:
    runs_dir = Path(a.runs_dir)
    landed_tasks = [
        (p, run_dir.name, p.stem)
        for run_dir in sorted(d for d in runs_dir.glob("*") if d.is_dir())
        for p in sorted(run_dir.glob("tasks/*/*.json"))
        if _json_or(_read_text_or_none(p), {}).get("landed") is True
    ]
    landed = [(run, task) for p, run, task in landed_tasks if _in_window(p, a.since)]
    usage_calls = [
        {**call, "run": run_id}
        for run_id, u in run_store.usages(runs_dir).items() if _run_in_window(runs_dir, run_id, a.since)
        for call in (u.get("calls") or [])
    ]
    land_rows = [r for line in (_read_text_or_none(runs_dir / "land.jsonl") or "").splitlines() if isinstance(r := _json_or(line, None), dict)]
    prices = _chair_catalog_prices(a)
    sessions = stats_chair.session_ids(chair.read(runs_dir) if (runs_dir / chair.CHAIR_FILENAME).exists() else None, a.session or [])
    lines = [line for s in sessions for line in _chair_transcript_lines(Path(a.projects_dir).expanduser(), s)]
    chair_usd = stats_chair.chair_cost(stats_chair.lines_since(lines, a.since), prices) if prices else None
    items = [
        stats_chair.frontmatter_item(text, p.stem)
        for p in sorted(Path(a.work_store_root).glob("*/*/*.md")) if p.name != "initiative.md"
        if (text := _read_text_or_none(p)) is not None
    ]
    report = stats_chair.chair_report(a.since, landed, usage_calls, land_rows, chair_usd, items)
    print(json.dumps(report, indent=2) if a.json else stats_chair.render_chair(report))
    return 0


def _stats_examples(a: argparse.Namespace) -> int:
    roles = list(dict.fromkeys(a.role))
    for role in roles:
        if role not in stats_examples.BUILDERS:
            print(f"unknown role {role!r}: choose one of {', '.join(stats_examples.BUILDERS)}", file=sys.stderr)
            return 2
    texts = [_read_text_or_none(p) for p in sorted(Path(a.runs_dir).glob("*/tasks/*/*.json"))]
    records = [stats_examples.parse_record(t) if t is not None else None for t in texts]
    built = [(role, stats_examples.build_examples(records, role, a.since)) for role in roles]
    out = "".join(stats_examples.format_lines(examples) for _, examples in built)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        sys.stdout.write(out)
    counts = [f"written {len(ex)}, skipped {len(records) - len(ex)}" for _, ex in built]
    if len(built) == 1:
        print(f"read {len(records)}, {counts[0]}", file=sys.stderr)
    else:
        print(f"read {len(records)}; " + "; ".join(f"{r}: {c}" for (r, _), c in zip(built, counts)), file=sys.stderr)
    return 0


def _stats_system_one(a: argparse.Namespace) -> int:
    usages = list(run_store.usages(Path(a.runs_dir)).values())
    found = stats_system_one.summaries(stats_system_one.rows_from_usage(usages), a.since, a.role)
    print(json.dumps(stats_system_one.to_json(found), indent=2) if a.json else stats_system_one.render_report(found))
    if a.propose:
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        for s in found:
            if stats_system_one.verdict(s)[0] != "READY":
                print(f"skipped {s.role}: not ready", file=sys.stderr)
                continue
            out = Path(a.plans_dir) / f"system-one-graduation-{s.role}-{today}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(stats_system_one.render_proposal(s, today), encoding="utf-8")
            print(f"wrote {out}", file=sys.stderr)
    return 0


def _bounds_ceiling_for(a: argparse.Namespace):
    """role -> declared ceiling from the resolved provider profile's
    `role_budget_usd` (per-role) falling back to its `budget_usd` (default);
    `None` for both when the routing profile at `_profile_path(a)` (spec §1:
    `--profile`, `$AGENT_TOOLS_PROFILE` or `DEFAULT_PROFILE`, same as `route
    context`/`setup doctor`) or its named `provider_profile` is missing or
    unreadable — reported, never refused, matching `_gather_doctor_facts`'s
    style rather than `_resolve_profile_or_refuse`'s (docs/design/
    cost-bounds.md §2: 'the declared ceiling from the resolved provider
    profile')."""
    text = _read_text_or_none(_profile_path(a))
    try:
        routing_profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        routing_profile = {}
    provider_path = routing_profile.get("provider_profile")
    provider_text = _read_text_or_none(Path(provider_path).expanduser()) if provider_path else None
    try:
        provider_profile = yaml.safe_load(provider_text) if provider_text is not None else None
    except yaml.YAMLError:
        provider_profile = None
    provider_profile = provider_profile if isinstance(provider_profile, dict) else {}
    role_budget_usd = provider_profile.get("role_budget_usd") or {}
    budget_usd = provider_profile.get("budget_usd")
    budget_usd = budget_usd if isinstance(budget_usd, dict) else {}
    tier_overrides = provider_profile.get("tier_overrides") or {}
    defaults = provider_profile.get("defaults") or {}

    def _ceiling(role, _model):
        tier = _resolved_tier(tier_overrides, defaults, role)
        value = role_budget_usd[role] if role in role_budget_usd else budget_usd.get(tier)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return _ceiling


# A profile may name a capability class or a tier; tools does not import cartridges.
_CLASS_TIER = {"extract": "cheap", "reason": "standard", "judge": "deep", "frontier": "deep"}


def _resolved_tier(tier_overrides: dict, defaults: dict, role: str) -> str:
    """A role's tier: its own `tier_overrides` entry, else the profile's
    `defaults` entry, else `"standard"` — the chain `_bounds_ceiling_for`
    and `_router_policy_for` both resolve a role's tier through. An entry
    may name a tier or a capability class; the result is always a tier."""
    raw = tier_overrides.get(role, defaults.get(role, "standard"))
    tier = _CLASS_TIER.get(raw, raw) if isinstance(raw, str) else None
    return tier if tier in _CLASS_TIER.values() else "standard"


def _stats_bounds(a: argparse.Namespace) -> int:
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.bounds_report(_stats_fetch(conn, "calls"), _bounds_ceiling_for(a))
    finally:
        conn.close()
    if a.level:
        drop = {"strict", "moderate", "liberal"} - {a.level}
        report = [{k: v for k, v in row.items() if k not in drop} for row in report]
    if a.write:
        write_path = Path(a.write).resolve()
        repo_root = Path.cwd().resolve()
        if write_path != repo_root and repo_root not in write_path.parents:
            print(f"stats bounds: --write path {write_path} is outside the repo checkout {repo_root}")
            return 2
        generated = datetime.datetime.now(datetime.UTC).isoformat()
        write_path.write_text(json.dumps({"generated": generated, "db": a.db, "rows": report}, indent=2))
    print(json.dumps(report, indent=2) if a.json else stats_query.render_capped(report))
    return 0


def _router_policy_for(a: argparse.Namespace, role: str) -> dict | None:
    """Routing inputs for `role`: `policy["mode"]` is the routing profile's
    own `router` key (off|shadow|on), defaulting to `"off"` when absent
    since router-steward.md §1 names the three values but not a default.
    `policy["default_tier"]` resolves the same chain `_bounds_ceiling_for`
    resolves a role's tier through: `tier_overrides`, then `defaults`, then
    `"standard"`. `None` when the routing profile at `_profile_path(a)` or
    its `provider_profile` is missing or unparseable, so the caller can
    refuse rather than invent a policy."""
    text = _read_text_or_none(_profile_path(a))
    try:
        routing_profile = route.parse_profile(text) if text is not None else None
    except route.ProfileError:
        routing_profile = None
    if routing_profile is None:
        return None
    provider_path = routing_profile.get("provider_profile")
    provider_text = _read_text_or_none(Path(provider_path).expanduser()) if provider_path else None
    try:
        provider_profile = yaml.safe_load(provider_text) if provider_text is not None else None
    except yaml.YAMLError:
        provider_profile = None
    if not isinstance(provider_profile, dict):
        return None
    tier_overrides = provider_profile.get("tier_overrides") or {}
    defaults = provider_profile.get("defaults") or {}
    return {
        "mode": routing_profile.get("router", "off"),
        "default_tier": _resolved_tier(tier_overrides, defaults, role),
        "min_n": 20,
        "deviation_floor": 0.70,
        "challenger_n": 10,
    }


def _stats_window_from_report(report: list[dict], role: str) -> dict:
    """`stats_window` for `role`, sliced from `stats_query.roles_report`'s
    existing output rather than a new aggregation: `n` sums `n_calls`
    across every row for the role, challenger rows included
    (router-steward.md §1: "n counts every call of that role in the
    window, challenger calls included, so the section 2 schedule lands on
    every Nth call"). `landed_rate` is the role's non-challenger rows'
    `landed_rate` weighted by `n_calls`, 0.0 when none carry one
    (router-steward.md §3 excludes challenger rows from that aggregate).
    `start`/`end` are `None`: no `roles_report` row carries a window
    timestamp, and `select_tier` never reads either field."""
    rows = [row for row in report if row["role"] == role]
    n = sum(row["n_calls"] for row in rows)
    floor_rows = [row for row in rows if not row["challenger"]]
    rated = [row for row in floor_rows if row["landed_rate"] is not None]
    weight = sum(row["n_calls"] for row in rated)
    landed_rate = sum(row["landed_rate"] * row["n_calls"] for row in rated) / weight if weight else 0.0
    return {"landed_rate": landed_rate, "n": n, "start": None, "end": None}


def _router_stats_window_for(a: argparse.Namespace, role: str) -> dict:
    """The edge for `_stats_window_from_report`: opens `a.db` and fetches
    the same `roles_report` inputs `cox stats roles` reads, no new
    aggregation added to `stats_query.py`."""
    conn = stats_schema.connect(a.db)
    try:
        report = stats_query.roles_report(_stats_fetch(conn, "calls"), _stats_fetch(conn, "tasks"), _stats_fetch(conn, "runs"))
    finally:
        conn.close()
    return _stats_window_from_report(report, role)


def _router_select(a: argparse.Namespace) -> int:
    """`cox router select` — the CLI edge of docs/design/router-steward.md:
    `router.select_tier` stays pure; every read happens here. `off` returns
    the floor without calling `select_tier`. `shadow` calls it, prints the
    would-be decision marked as not authoritative, and still returns the
    floor as the effective answer. `on` calls it and returns its tier as
    the effective, authoritative answer."""
    policy = _router_policy_for(a, a.role)
    if policy is None:
        print(f"router select: no usable profile at {_profile_path(a)}")
        return 2
    mode = policy["mode"]
    floor = policy["default_tier"]
    if mode == "off":
        effective, reason = floor, "off"
    else:
        stats_window = _router_stats_window_for(a, a.role)
        tier, tier_reason = router.select_tier(a.role, stats_window, policy)
        if mode == "shadow":
            print(f"router select: shadow tier={tier} reason={tier_reason} (not authoritative; effective stays {floor})")
            effective, reason = floor, "shadow"
        else:
            effective, reason = tier, tier_reason
    if a.json:
        print(json.dumps({"role": a.role, "mode": mode, "effective_tier": effective, "reason": reason}))
    else:
        print(f"router select: role={a.role} mode={mode} effective_tier={effective} reason={reason}")
    return 0


def _steward_recent_run_ids(pair_calls: list[dict], run_span: dict, *, day_cap: int, run_cap: int) -> set:
    """Run ids for `pair_calls`, newest-first by `ended_at` (falling back to
    `started_at`), kept up to `run_cap` runs or until a run's `started_at`
    falls more than `day_cap` days before the newest kept run's own end —
    whichever limit is hit first (router-steward.md §5: "a window capped at
    14 days or 20 runs, whichever completes first"). Measured from the
    pair's own latest run, never the wall clock, so a pair that has simply
    existed a long time is not disqualified by history it no longer needs."""
    ordered = sorted(
        {c["run_id"] for c in pair_calls if c.get("run_id") in run_span},
        key=lambda rid: run_span[rid][1] or run_span[rid][0] or "",
        reverse=True,
    )
    if not ordered:
        return set()
    newest_end = run_span[ordered[0]][1] or run_span[ordered[0]][0]
    cutoff = datetime.datetime.fromisoformat(newest_end) - datetime.timedelta(days=day_cap) if newest_end else None
    kept = []
    for rid in ordered:
        if len(kept) >= run_cap:
            break
        started_at = run_span[rid][0]
        if cutoff is not None and started_at is not None and datetime.datetime.fromisoformat(started_at) < cutoff:
            break
        kept.append(rid)
    return set(kept)


def _steward_window_days_for(run_ids: set, run_span: dict) -> int:
    """Days between the earliest `started_at` and the latest `ended_at`
    among `run_ids`; `0` when neither parses."""
    starts = [run_span[rid][0] for rid in run_ids if run_span.get(rid, (None, None))[0]]
    ends = [run_span[rid][1] for rid in run_ids if run_span.get(rid, (None, None))[1]]
    if not starts or not ends:
        return 0
    start = datetime.datetime.fromisoformat(min(starts))
    end = datetime.datetime.fromisoformat(max(ends))
    return (end - start).days


def _steward_candidate_rows(bounds: list[dict], calls: list[dict], tasks: list[dict], runs: list[dict]) -> list[dict]:
    """Per (role, model) in `bounds`: `stats_query.roles_report`'s
    challenger/floor split and `n_calls`, recomputed over only that pair's
    own most recent 20 runs or 14 days (`_steward_recent_run_ids`) rather
    than its whole history, joined onto `bound`'s ceiling/censored/strict/
    moderate/liberal. A pair with no runs in that window, no challenger
    row, no floor row, or either row's `landed_rate` is dropped: the
    evidence bar cannot score a delta it cannot compute. Pure: no file, no
    db connection, no clock read — `calls`/`tasks`/`runs` arrive already
    fetched."""
    run_span = {r["run_id"]: (r.get("started_at"), r.get("ended_at")) for r in runs}
    rows = []
    for bound in bounds:
        role, model = bound["role"], bound["model"]
        pair_calls = [c for c in calls if c.get("role") == role and c.get("model") == model]
        recent_run_ids = _steward_recent_run_ids(pair_calls, run_span, day_cap=14, run_cap=20)
        if not recent_run_ids:
            continue
        recent_calls = [c for c in pair_calls if c.get("run_id") in recent_run_ids]
        recent_tasks = [t for t in tasks if t.get("run_id") in recent_run_ids]
        windowed = stats_query.roles_report(recent_calls, recent_tasks, runs)
        challenger_row = next((r for r in windowed if r["role"] == role and r["model"] == model and r["challenger"]), None)
        floor_row = next((r for r in windowed if r["role"] == role and r["model"] == model and not r["challenger"]), None)
        if challenger_row is None or floor_row is None:
            continue
        if challenger_row["landed_rate"] is None or floor_row["landed_rate"] is None:
            continue
        rows.append({
            **bound,
            "n_challenger": challenger_row["n_calls"],
            "landed_rate_challenger": challenger_row["landed_rate"],
            "landed_rate_floor": floor_row["landed_rate"],
            "window_days": _steward_window_days_for(recent_run_ids, run_span),
        })
    return rows


def _steward_bounds_rows_for(a: argparse.Namespace) -> list[dict]:
    """Edge for `_steward_candidate_rows`: opens `a.db` once, fetches
    `calls`/`tasks`/`runs` and the read-only ceiling from
    `_bounds_ceiling_for`, then hands everything to the pure join. Writes
    nothing."""
    conn = stats_schema.connect(a.db)
    try:
        calls = _stats_fetch(conn, "calls")
        tasks = _stats_fetch(conn, "tasks")
        runs = _stats_fetch(conn, "runs")
    finally:
        conn.close()
    bounds = stats_query.bounds_report(calls, _bounds_ceiling_for(a))
    return _steward_candidate_rows(bounds, calls, tasks, runs)


def _steward_propose(a: argparse.Namespace) -> int:
    """`cox steward propose` — the CLI edge of docs/design/router-steward.md
    §4: reads `a.db` and the routing/provider profile read-only to build
    candidate rows, then writes each candidate that clears §5's bar as a
    new intake file via the same writer `route file --intake` uses. It
    never opens a provider profile to write it; its only writes are new
    files under `workspace_dir/intake`."""
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    policy = {"min_n": 20, "window_days_cap": 14, "landed_rate_delta_floor": 0.05}
    candidates = steward.ceiling_candidates(_steward_bounds_rows_for(a), policy)
    if not candidates:
        if a.json:
            print(json.dumps([]))
        else:
            print("steward propose: [] (no candidate cleared the evidence bar)")
        return 0
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    mapping: dict[str, str] = {}
    for candidate in candidates:
        title = f"steward: {candidate['direction']} {candidate['role']}'s ceiling for {candidate['model']}"
        mapping.update(route.intake_file(title, steward.render_proposal(candidate), "coxswain-tools", date))
    targets = {rel: ws / rel for rel in mapping}
    existing = [str(path) for path in targets.values() if path.exists()]
    if existing:
        print(f"routing: refusing to overwrite existing path(s): {', '.join(existing)}")
        return 2
    for rel, path in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mapping[rel], encoding="utf-8")
    written = [str(targets[rel]) for rel in sorted(mapping)]
    print(json.dumps(written) if a.json else "\n".join(written))
    return 0


def _steward_draft(a: argparse.Namespace) -> int:
    """`cox steward draft` — writes grounded steward proposals as draft initiatives; see steward_draft."""
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    return steward_draft.run_draft(Path(profile["workspace_dir"]).expanduser(), as_json=a.json)


def _runs_top(a: argparse.Namespace) -> int:
    heartbeat_minutes = _leader_heartbeat_minutes()
    if a.once:
        chair_state = runs_top_screen.chair_now(a.runs_dir, heartbeat_minutes)
        print("\n".join(runs_top.render(runs_top_screen.rows_now(a.runs_dir, heartbeat_minutes), 120, chair_state)))
        return 0
    if not sys.stdin.isatty():
        print("runs top: needs a terminal; use --once")
        return 2
    return runs_top_screen.main(a.runs_dir, a.interval, heartbeat_minutes)


def _runs_bar(a: argparse.Namespace) -> int:
    rows = [
        dataclasses.asdict(runs_top.row(f["run"], f["alive"], f["phases"], f["events"], f["calls"], f["ceiling"], f["launched_by"]))
        for f in runs_top_screen.facts(a.runs_dir)
    ]
    print(json.dumps(runs_bar.bar(rows, runs_bar.attention(rows))))
    return 0


def _resolved_notify_policy(runs_dir: Path) -> dict:
    """`policy.notify` from `<runs_dir>/policy.notify.json` when present, else DEFAULT_POLICY, field by field."""
    default = notify.DEFAULT_POLICY
    text = _read_text_or_none(runs_dir / "policy.notify.json")
    if text is None:
        return default
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return default
    if not isinstance(raw, dict):
        return default
    return {"kinds": raw.get("kinds", default["kinds"]), "min_cost_usd": raw.get("min_cost_usd", default["min_cost_usd"])}


def _runs_notify(a: argparse.Namespace) -> int:
    policy = _resolved_notify_policy(Path(a.runs_dir))
    return notify.run_loop(a.runs_dir, once=a.once, interval=a.interval, policy=policy,
                            heartbeat_minutes=_leader_heartbeat_minutes(), replay=a.replay)


def _runs_detail(a: argparse.Namespace) -> int:
    d = runs_detail.detail(**runs_detail_screen.facts_for(a.runs_dir, a.run_id))
    if a.json:
        print(json.dumps(dataclasses.asdict(d)))
    else:
        print("\n".join(runs_detail.render(d, 120)))
    return 0


def _runs_stranded(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"stranded: {reason}")
        return 2
    task_records = []
    for path in sorted(runs_dir.glob("*/tasks/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.setdefault("run", path.parent.parent.parent.name)
        record.setdefault("task", path.stem)
        record.setdefault("phase", path.parent.name)
        task_records.append(record)
    ws = runs_dir.parent
    initiative_texts = _initiative_texts(ws)
    items = [
        {**item, "repo": route.parse_frontmatter(initiative_texts.get(item["initiative"], ""))[0].get("repo")}
        for item in _work_items(ws)
    ]
    rows = runs_stranded.stranded(task_records, items, os.path.isdir)
    missing = runs_stranded.missing_repos(task_records, items, os.path.isdir)

    def report_missing() -> None:
        if missing:
            print(f"skipped missing repo: {', '.join(missing)}", file=sys.stderr)

    if a.json:
        print(json.dumps(rows))
        report_missing()
        return 0
    if not rows:
        print("no stranded work")
        report_missing()
        return 0
    print(records.format_table(rows, ["run", "task", "phase", "branch", "remedy"]))
    report_missing()
    return 0


def _runs_cause(a: argparse.Namespace) -> int:
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"cause: {reason}")
        return 2
    record, where, found = _land_record(runs_dir, a.run_id, a.task_id)
    if record is None:
        print(f"cause: expected one task record for {a.task_id} under {where}, found {found}")
        return 2
    items = sorted((runs_dir.parent / "work").glob(f"*/{record['phase']}/{a.task_id}.md"))
    if len(items) != 1:
        print(f"cause: expected one work item {a.task_id} in phase {record['phase']}, found {len(items)}")
        return 2
    updated = stats_chair.set_attempt_cause(items[0].read_text(encoding="utf-8"), a.run_id, a.cause, a.note)
    if updated is None:
        print(f"cause: {items[0]} has no attempts entry for run {a.run_id}")
        return 2
    items[0].write_text(updated, encoding="utf-8")
    print(f"{a.task_id}: attempt {a.run_id} cause = {a.cause}")
    return 0


def _stored_trace_calls(calls: list[dict], role: str | None) -> list[tuple[str, dict]]:
    """Usage calls as (`<role>-<n>`, call), numbered per role in usage order before `role` filters, sorted like the loose files."""
    named = [
        (f"{c.get('role')}-{1 + sum(1 for p in calls[:i] if p.get('role') == c.get('role'))}", c)
        for i, c in enumerate(calls)
    ]
    kept = [(node, c) for node, c in named if role is None or c.get("role") == role]
    return sorted(kept, key=lambda t: (t[0].rsplit("-", 1)[0], int(t[0].rsplit("-", 1)[1])))


def _print_trace(a: argparse.Namespace, nodes: list[tuple[str, dict]]) -> int:
    """Print the -v detail per node, then the table. `nodes` is (node name, `records.trace_summary` result)."""
    rows = []
    for node, s in nodes:
        rows.append({"node": node, "turns": s["turns"] or 0, "cost_usd": float(s["cost_usd"] or 0), "result": s["subtype"] or "?",
                     "bash": s["tools"].get("Bash", 0), "reads": sum(s["reads"].values()), "whole": s["whole_file_reads"]})
        if a.verbose:
            print(f"== {node}: tools={s['tools']} reads={s['reads']} whole_file_reads={s['whole_file_reads']}")
            for c in s["commands"][:12]:
                print("   $", c)
    print(records.format_table(rows, ["node", "turns", "cost_usd", "result", "bash", "reads", "whole"]))
    return 0


def _runs_trace(a: argparse.Namespace) -> int:
    d = Path(a.runs_dir) / f"{a.run_id}-trace"
    files = sorted(d.glob(f"{a.role}-*.jsonl" if a.role else "*.jsonl"), key=lambda p: (p.stem.rsplit("-", 1)[0], int(p.stem.rsplit("-", 1)[1])))
    if files:
        return _print_trace(a, [(f.stem, records.trace_summary(records.load_trace(f))) for f in files])
    # The loose files are gone (or the dir is empty) once a finished run is compacted: read its calls from the usage record and the trace store.
    usage = run_store.usage(Path(a.runs_dir), a.run_id)
    if not usage or not usage.get("calls"):
        print(f"no trace for {a.run_id} in {a.runs_dir}")
        return 2
    nodes: list[tuple[str, dict]] = []
    for node, call in _stored_trace_calls(list(usage["calls"]), a.role):
        stored = records.summary_from_call(call)
        if stored is not None:
            nodes.append((node, stored))
            continue
        try:
            ev = run_store.call_events(Path(a.runs_dir), a.run_id, call)
        except run_store.TracesUnavailable as exc:
            print(exc)
            return 2
        if ev is not None:
            nodes.append((node, records.trace_summary(ev)))
    return _print_trace(a, nodes)


def _runs_clean(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"clean: {reason}")
        return 2
    records = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in (runs_dir / a.run_id / "tasks").glob("*/*.json")}
    landed = {task for task, r in records.items() if r.get("landed")}
    dropped = {task for task, r in records.items() if r.get("status") == "dropped"}
    reasons = {task: r.get("reason", "unlanded") for task, r in records.items()}
    p = cleanup.plan_cleanup(run_id=a.run_id, worktrees=cleanup.git_worktrees(repo), branches=cleanup.git_branches(repo), worktree_root=a.worktree_root)
    for line in cleanup.apply_cleanup(repo, p, dry_run=not a.apply, landed=landed, dropped=dropped, reasons=reasons, force=a.force):
        print(line)
    if not a.apply:
        print("(dry run — pass --apply to do it)")
    return 0


def _runs_dir_for_land(a: argparse.Namespace) -> tuple[Path | None, str | None]:
    """`--runs-dir` overrides; otherwise `runs/` under the profile's `workspace_dir`."""
    if a.runs_dir:
        return Path(a.runs_dir).expanduser(), None
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        return None, f"no profile at {profile_path}"
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return None, f"profile unreadable: {exc}"
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        return None, f"workspace_dir not set in profile {profile_path}"
    return Path(workspace).expanduser() / "runs", None


def _land_record(runs_dir: Path, run_id: str, task: str | None) -> tuple[dict | None, str, int]:
    tasks_root = runs_dir / run_id / "tasks"
    matches = sorted(tasks_root.glob(f"*/{task}.json" if task else "*/*.json"))
    if len(matches) != 1:
        return None, str(tasks_root.resolve()), len(matches)
    path = matches[0]
    record = json.loads(path.read_text(encoding="utf-8"))
    record.setdefault("run", run_id)
    record.setdefault("task", path.stem)
    record.setdefault("phase", path.parent.name)
    return record, str(path), 1


def _choose_record(stored: dict | None, filed: dict | None) -> tuple[dict | None, str | None]:
    """The store record wins over the file record; the label is `store` or `file`, None when neither exists."""
    if stored is not None:
        return stored, "store"
    if filed is not None:
        return filed, "file"
    return None, None


def _land_load(runs_dir: Path, run_id: str, task: str | None) -> tuple[dict | None, str, int, str | None]:
    """Edge: `(record, where, count, source)`, the store first and the file when the store says None.

    The store is asked by phase when exactly one file names the task, whose parent dir is the phase and stem the task.
    With no file and a task given, it is asked which phases hold the task; exactly one phase names a record
    that has no file yet, and `where` is the path that file would have.
    """
    filed, where, count = _land_record(runs_dir, run_id, task)
    if count == 0 and task:
        phases = run_store.task_record_phases(runs_dir, run_id, task)
        held = run_store.task_record(runs_dir, run_id, phases[0], task) if len(phases) == 1 else None
        if held is not None:
            path = runs_dir / run_id / "tasks" / phases[0] / f"{task}.json"
            return {"run": run_id, "task": task, "phase": phases[0], **held}, str(path), 1, "store"
        return None, where, count, None
    path = Path(where)
    stored = run_store.task_record(runs_dir, run_id, path.parent.name, path.stem) if count == 1 else None
    if stored is not None:
        stored = {"run": run_id, "task": path.stem, "phase": path.parent.name, **stored}
    record, source = _choose_record(stored, filed)
    return record, where, count, source


def _initiative_of(work_root: Path, task_id: str) -> str | None:
    """The one initiative whose `work/<initiative>/<phase>/*.md` holds an item
    with this id (`id` falls back to the file stem, as `route.work_item` does),
    or `None` when none or several do. The task record never names it."""
    found = set()
    for p in work_root.glob("*/*/*.md"):
        text = _read_text_or_none(p)
        if text is None:
            continue
        item = route.work_item(route.parse_frontmatter(text)[0], initiative=p.parent.parent.name, phase_dir=p.parent.name, stem=p.stem)
        if item["id"] == task_id:
            found.add(item["initiative"])
    return found.pop() if len(found) == 1 else None


def _initiative_of_phase(work_root: Path, phase: str) -> str | None:
    """The one initiative whose `work/<initiative>/<phase>/` holds a ticket, or
    `None` when none or several do. The phase record may not name it."""
    found = {p.parent.parent.name for p in work_root.glob(f"*/{phase}/*.md") if p.name != "initiative.md"}
    return found.pop() if len(found) == 1 else None


def _land_branches(repo: Path, record: dict, default_branch: str) -> dict[str, list[str]]:
    """Commit subjects ahead of `default_branch`, per candidate branch, with
    merge commits excluded by `git` (`--no-merges`) and patch-equivalent
    commits already on `default_branch` dropped (`--cherry-pick`)."""
    candidates = [f"agents/{record['run']}/{record['task']}", f"epic/{record.get('initiative')}/{record['phase']}"]
    branches: dict[str, list[str]] = {}
    for b in candidates:
        out = subprocess.run(["git", "-C", str(repo), "log", "--no-merges", "--cherry-pick", "--right-only", "--format=%s", f"{default_branch}...{b}"],
                             capture_output=True, text=True)
        if out.returncode == 0:
            branches[b] = [line for line in out.stdout.splitlines() if line]
    return branches


def _recover_branches(repo: Path, record: dict, phase_branch: str) -> dict[str, list[str]]:
    """Commit subjects ahead of the phase branch, per recover candidate
    branch — the same `git log --no-merges` shape `_land_branches` uses, but
    against the phase branch instead of the default branch, since recover
    merges INTO the phase branch rather than cherry-picking off it."""
    candidates = [f"agents/{record['run']}/{record['task']}", f"epic/{record.get('initiative')}/{record['phase']}--{record['task']}"]
    branches: dict[str, list[str]] = {}
    for b in candidates:
        out = subprocess.run(["git", "-C", str(repo), "log", "--no-merges", "--format=%s", f"{phase_branch}..{b}"], capture_output=True, text=True)
        if out.returncode == 0:
            branches[b] = [line for line in out.stdout.splitlines() if line]
    return branches


def _branch_exists(repo: Path, branch: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", branch], capture_output=True, text=True)
    return r.returncode == 0


def _git_out(repo: Path, *args: str) -> str | None:
    """Stripped stdout of `git -C repo <args>`, or `None` when git exits non-zero."""
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _open_prs_for(repo: Path, branch: str, forge_module=forge_github) -> list[int] | str:
    return forge_module.find_open_prs(repo, branch)


def _land_resume(repo: Path, cherry_pick: dict, forge_module=forge_github) -> dict:
    """`land.resume_decision` for the `pr/<task>` branch a `cherry_pick` step
    would create. The expected tree is what cherry-picking the one commit onto
    `from` yields, computed by `git merge-tree` without touching the checkout.
    Open PRs are only asked for once every existing branch already matches."""
    branch, base = cherry_pick["onto"], cherry_pick["from"]
    local = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{tree}}")
    fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                             capture_output=True, text=True).returncode == 0
    remote = _git_out(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{tree}}") if fetched else None
    if local is None and remote is None:
        return land.resume_decision("", None, None, [])
    shas = (_git_out(repo, "rev-list", f"{base}..{cherry_pick['branch']}") or "").split()
    merged = _git_out(repo, "merge-tree", "--write-tree", f"--merge-base={shas[0]}^", base, shas[0]) if len(shas) == 1 else None
    expected = merged.splitlines()[0] if merged else None
    if expected is None:
        return {"kind": "refuse", "reason": f"cannot compute the cherry-picked tree of {cherry_pick['branch']} onto {base}"}
    if _git_out(repo, "cat-file", "-e", f"{expected}:.agent-generate") is not None:
        # The pushed tree is the one after generation and the amend, not the bare cherry-pick.
        expected, why = _generated_tree(repo, branch, base, shas[0], cherry_pick.get("umbrella"))
        if expected is None:
            return {"kind": "refuse", "reason": f"cannot compute the generated tree of {cherry_pick['branch']} onto {base}: {why}"}
    existing = [t for t in (local, remote) if t is not None]
    prs = _open_prs_for(repo, branch, forge_module) if all(t == expected for t in existing) else []
    if isinstance(prs, str):
        return {"kind": "refuse", "reason": prs}
    decision = land.resume_decision(expected, local, remote, prs)
    if decision["kind"] != "refuse":
        return decision
    files = sorted({f for t in existing if t != expected
                    for f in (_git_out(repo, "diff-tree", "-r", "--name-only", expected, t) or "").split()})
    return {**decision, "reason": decision["reason"] + (f"; files differing: {', '.join(files)}" if files else "")}


def _land_enrich(steps: list[dict], *, path: str, worktree_root: str, task_paths: dict[str, str] | None = None,
                  item_path: str | None = None, workspace: str | None = None,
                  item_id: str | None = None, profile: str | None = None,
                  runs_dir: str | None = None, umbrella: str | None = None) -> list[dict]:
    """Steps enriched with what only the edge knows: each `mark_done`'s own
    record file path (`task_paths` maps task name to path in phase mode), and
    the configured worktree root for `clean`/`clean_phase`. `item_path` (task
    mode only) is the work item `mark_done` will also try to close out. A
    `checks` step that names a `branch` (phase mode) gets no filesystem write
    here — a dry run must stay read-only, so the actual worktree is only ever
    created at execution time, inside `_execute_land_step`, under `--apply`.
    A `route_sync` step gets the `workspace` to sync, the item's own `id`
    (`item_id`, else the task name the plan used), and the land's `profile`
    and `runs_dir`, so the step reads the tracker from where the plan did.
    A `cherry_pick` step gets the profile's `umbrella_dir` as `umbrella` when set, for `.agent-generate`."""
    def enrich(step: dict) -> dict:
        if step["kind"] == "cherry_pick" and umbrella is not None:
            return {**step, "umbrella": umbrella}
        if step["kind"] == "route_sync":
            return {**step, "item": item_id or step["item"], "workspace": workspace,
                    "profile": profile, "runs_dir": runs_dir}
        if step["kind"] == "mark_done":
            marked = {**step, "path": (task_paths or {}).get(step["task"], path)}
            return {**marked, "item": item_path, "from": "approved", "to": "done"} if item_path else marked
        if step["kind"] in ("clean", "clean_phase"):
            return {**step, "worktree_root": worktree_root}
        return step
    return [enrich(s) for s in steps]


def _land_phase_record(runs_dir: Path, run_id: str, phase: str) -> tuple[dict | None, list[dict], dict[str, str], str]:
    """The phase record at `runs/<run>:<phase>.json` and every task record
    filed under it, keyed by task name for `mark_done`'s own file path."""
    phase_path = runs_dir / f"{run_id}:{phase}.json"
    phase_record = next((m for m in run_store.phase_manifests(runs_dir, run_id) if m.get("run_id") == f"{run_id}:{phase}"), None)
    if phase_record is None and phase_path.exists():  # a file whose body carries no run_id
        phase_record = json.loads(phase_path.read_text(encoding="utf-8"))
    if phase_record is None:
        return None, [], {}, str(phase_path.resolve())
    phase_record.setdefault("run", run_id)
    phase_record.setdefault("phase", phase)
    task_records, task_paths = [], {}
    for p in sorted((runs_dir / run_id / "tasks" / phase).glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        r.setdefault("run", run_id); r.setdefault("task", p.stem); r.setdefault("phase", phase)
        task_records.append(r)
        task_paths[r["task"]] = str(p)
    return phase_record, task_records, task_paths, str(phase_path)


def _phase_items(work_root: Path, initiative: str, phase: str) -> tuple[list[dict] | None, str]:
    """The phase's own tickets from the work store (`work/<initiative>/<phase>/*.md`,
    `route.work_item`'s `state`), or `None` with the directory searched when
    there is nothing there to read — a phase can never be waved through by a
    missing ticket file."""
    phase_dir = work_root / initiative / phase
    paths = sorted(phase_dir.glob("*.md")) if phase_dir.is_dir() else []
    if not paths:
        return None, str(phase_dir)
    items = []
    for p in paths:
        text = _read_text_or_none(p)
        if text is None:
            continue
        fields = route.parse_frontmatter(text)[0]
        item = route.work_item(fields, initiative=initiative, phase_dir=phase, stem=p.stem)
        items.append({"id": item["id"], "status": item["state"]})
    return items, str(phase_dir)


def _phase_needing_land(runs_dir: Path, run_id: str) -> str | None:
    """The one phase under `runs/<run_id>/tasks/*/` holding more than one
    task record — phase mode's default trigger when neither `--phase` nor
    `--task` is given. `None` when no single phase qualifies, leaving task
    mode's own record count to speak (including its "found 0" refusal)."""
    tasks_root = runs_dir / run_id / "tasks"
    if not tasks_root.is_dir():
        return None
    candidates = [d.name for d in tasks_root.iterdir() if d.is_dir() and len(list(d.glob("*.json"))) > 1]
    return candidates[0] if len(candidates) == 1 else None


_LAUNCH_ERROR = land.LAUNCH_ERROR


def _run_checks(checks: list[tuple[str, list[str]]], cwd: Path) -> tuple[bool, str]:
    """Each `(name, argv)` pair in order, stopping at the first launch error
    or failure and naming it by `name`, never by its argv or shell command."""
    for name, argv in checks:
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        except OSError as exc:
            return False, f"{_LAUNCH_ERROR}{name}: {exc}"
        if r.returncode != 0:
            return False, f"{name}: {(r.stderr or r.stdout).strip()}"
    return True, f"{len(checks)} checks passed"


def _land_worktree(repo: Path, branch: str) -> Path:
    """Carries this process's pid: on 2026-09-27 two concurrent lands of one branch, the chair's and the loop's, deleted each other's worktree mid-check."""
    return Path(tempfile.gettempdir()) / f"cox-land-{Path(repo).resolve().name}-{branch.replace('/', '-')}-{os.getpid()}"


def _link_venv(repo: Path, wt: Path) -> None:
    """Point `wt/.venv` at the checkout's, so a check naming `.venv/bin/python` runs the worktree's code in the repo's environment."""
    venv = Path(repo).resolve() / ".venv"
    if venv.is_dir() and not (wt / ".venv").exists():
        (wt / ".venv").symlink_to(venv, target_is_directory=True)


_GENERATE_FAILED = "generate failed: "


def _generate_and_amend(wt: Path, umbrella: str | None, echo: Callable[[str], None] = print) -> tuple[bool, str]:
    """Run the worktree's `.agent-generate`, then fold any change it made into the one commit.

    `(True, "")` when the file is absent or nothing changed; `(False, output)` on the first non-zero exit."""
    spec = wt / ".agent-generate"
    if not spec.is_file():
        return True, ""
    plan = generate.plan_commands(generate.parse_generate_file(spec.read_text(encoding="utf-8")), umbrella)
    for _, note in plan:
        if note is not None:
            echo(f"land: {note}")
    env = {**os.environ, **generate.build_env(str(wt), umbrella)}
    for command in (c for c, note in plan if note is None):
        r = subprocess.run(command, shell=True, cwd=wt, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            return False, f"{_GENERATE_FAILED}{command!r} exited {r.returncode}: {r.stdout.strip()}"
    # `_link_venv`'s `.venv` is a symlink, which a `.venv/` ignore rule does not match: keep it out of the commit.
    outside_venv = ["--", ".", ":(exclude).venv"]
    if not _git_out(wt, "status", "--porcelain", *outside_venv):
        return True, ""
    # Not `add -A` with the exclude: git 2.55 refuses an exclude pathspec that names an ignored path. Stage all,
    # then take the linked `.venv` back out, which covers a git that ignores the symlink and one that does not.
    staged = subprocess.run(["git", "-C", str(wt), "add", "-A", "--", "."], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(wt), "reset", "-q", "--", ".venv"], capture_output=True, text=True)
    changed = (_git_out(wt, "diff", "--cached", "--name-only") or "").split()
    amend = subprocess.run(["git", "-C", str(wt), "commit", "--amend", "--no-edit"], capture_output=True, text=True)
    failed = next((r for r in (staged, amend) if r.returncode != 0), None)
    if failed is not None:
        return False, f"{_GENERATE_FAILED}{failed.stderr.strip() or failed.stdout.strip()}"
    echo(f"land: regenerated {' '.join(changed)}")
    return True, ""


def _cherry_pick_generated(wt: Path, sha: str, umbrella: str | None, echo: Callable[[str], None] = print) -> tuple[bool, str]:
    """Cherry-pick `sha` into `wt`, then regenerate and amend. The real land and the resume check both
    build the pushed tree here, so the tree the resume check expects cannot drift from the tree pushed."""
    cp = subprocess.run(
        ["git", "-C", str(wt), "-c", "merge.conflictStyle=diff3", "cherry-pick", sha], capture_output=True, text=True
    )
    if cp.returncode != 0:
        conflicted = subprocess.run(
            ["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True
        ).stdout.splitlines()

        def resolve(name: str) -> str | None:
            # A binary add/add leaves no markers, and a rename or directory/file conflict can leave no file:
            # neither is resolvable, so both take the abort path rather than escaping mid-cherry-pick.
            try:
                return land.resolve_add_add_conflicts((wt / name).read_text())
            except (OSError, UnicodeDecodeError):
                return None

        resolved = {name: resolve(name) for name in conflicted}
        if not conflicted or any(text is None for text in resolved.values()):
            subprocess.run(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True, text=True)
            return False, cp.stderr.strip() or cp.stdout.strip()
        for name, text in resolved.items():
            (wt / name).write_text(text)
        subprocess.run(["git", "-C", str(wt), "add", *sorted(resolved)], capture_output=True, text=True)
        cont = subprocess.run(
            ["git", "-C", str(wt), "-c", "core.editor=true", "cherry-pick", "--continue"],
            capture_output=True, text=True,
        )
        if cont.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "cherry-pick", "--abort"], capture_output=True, text=True)
            return False, cont.stderr.strip() or cont.stdout.strip()
        echo(f"resolved add/add: {', '.join(sorted(resolved))}")
    return _generate_and_amend(wt, umbrella, echo)


def _generated_tree(repo: Path, branch: str, base: str, sha: str, umbrella: str | None) -> tuple[str | None, str]:
    """The tree `_cherry_pick_generated` of `sha` onto `base` yields, or `(None, why)` when it cannot be built.

    Built at `branch`'s own land worktree path, so `COX_WORKTREE` matches the real land's. The generator runs
    again on a rerun, as it does on any land that is not resumed: it must be deterministic and idempotent,
    and its writes outside the worktree (the umbrella) repeat."""
    scratch = _land_worktree(repo, branch)
    _remove_land_worktree(repo, branch)
    try:
        add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(scratch), base], capture_output=True, text=True)
        if add.returncode != 0:
            return None, add.stderr.strip() or add.stdout.strip()
        _link_venv(repo, scratch)
        ok, detail = _cherry_pick_generated(scratch, sha, umbrella, echo=lambda _: None)
        return (_git_out(scratch, "rev-parse", "HEAD^{tree}"), "") if ok else (None, detail)
    finally:
        _remove_land_worktree(repo, branch)


def _remove_land_worktree(repo: Path, branch: str) -> None:
    """Drop `branch`'s land worktree and its registration; the branch itself stays."""
    wt = _land_worktree(repo, branch)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    # A leftover directory git no longer knows (a crashed land, another repo) would block `worktree add`.
    shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)


def _execute_land_step(repo: Path, step: dict, forge_module=forge_github) -> tuple[bool, str]:
    kind = step["kind"]
    if kind == "pick_branch":
        return True, f"{step['branch']} ({step['commit_subject']})"
    if kind == "cherry_pick":
        # The pr branch is built in its own worktree, so `repo`'s HEAD never moves.
        wt = _land_worktree(repo, step["onto"])
        _remove_land_worktree(repo, step["onto"])
        co = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", step["onto"], str(wt), step["from"]], capture_output=True, text=True)
        if co.returncode != 0:
            return False, co.stderr.strip() or co.stdout.strip()
        _link_venv(repo, wt)
        # The branch was chosen because it is exactly one commit ahead of
        # `from`, counted as `_land_branches` counts it: merges and commits
        # whose patch `from` already has (a stacked parent that merged as a
        # squash) are left out. So the range names the one commit without
        # matching on the subject text, which a second commit could share.
        rev = subprocess.run(["git", "-C", str(repo), "rev-list", "--no-merges", "--cherry-pick", "--right-only",
                              f"{step['from']}...{step['branch']}"], capture_output=True, text=True)
        shas = [s for s in rev.stdout.split() if s]
        if rev.returncode != 0 or len(shas) != 1:
            return False, f"expected exactly one commit ahead of {step['from']} on {step['branch']}, found {len(shas)}"
        ok, detail = _cherry_pick_generated(wt, shas[0], step.get("umbrella"))
        if not ok and detail.startswith(_GENERATE_FAILED):
            # Keep the worktree, detached, for inspection; free the branch so a rerun starts fresh
            # instead of finding a pr branch that holds the un-generated tree and refusing on it.
            subprocess.run(["git", "-C", str(wt), "checkout", "-q", "--detach"], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", step["onto"]], capture_output=True, text=True)
        return (True, f"cherry-picked {shas[0][:8]} onto {step['onto']}") if ok else (False, detail)
    if kind == "reuse_branch":
        wt = _land_worktree(repo, step["branch"])
        _remove_land_worktree(repo, step["branch"])
        args = ["worktree", "add", str(wt), step["branch"]] if step["local"] else ["worktree", "add", "-b", step["branch"], str(wt), f"origin/{step['branch']}"]
        co = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if co.returncode == 0:
            _link_venv(repo, wt)
        return co.returncode == 0, (f"reusing {step['branch']}" if co.returncode == 0 else co.stderr.strip() or co.stdout.strip())
    if kind == "checks":
        if "branch" in step:
            # Phase mode: never check out the phase branch in the working
            # repo (that races anything else using it) — build it fresh in
            # its own worktree instead, and remove it whatever happens. The
            # directory is allocated here, at execution, not in the plan
            # (`land.py`/`_land_enrich`), so a dry run never touches disk.
            wt = Path(tempfile.mkdtemp(prefix="land-checks-"))
            add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(wt), step["branch"]], capture_output=True, text=True)
            if add.returncode != 0:
                return False, add.stderr.strip() or add.stdout.strip()
            _link_venv(repo, wt)
            try:
                return _run_checks(step["checks"], wt)
            finally:
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        if "worktree_of" in step:
            return _run_checks(step["checks"], _land_worktree(repo, step["worktree_of"]))
        return _run_checks(step["checks"], repo)
    if kind == "push":
        return forge_module.push(repo, step["branch"])
    if kind == "pr_create":
        return forge_module.open_pr(repo, step["title"], step["body"], head=step.get("head"), base=step.get("base"))
    if kind == "wait_checks":
        return forge_module.wait_checks(repo, float(step.get("timeout_s", 180)), ref=step.get("branch", "HEAD"))
    if kind == "merge":
        return forge_module.merge(repo, step)
    if kind == "clean":
        wt = Path(step["worktree_root"]).expanduser() / step["run"] / step["task"]
        if wt.exists():
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "branch", "-D", step["branch"]], capture_output=True)
        return True, f"deleted branch {step['branch']}"
    if kind == "clean_phase":
        doomed = [b for b in cleanup.git_branches(repo) if b == step["phase_branch"] or b.startswith(step["phase_branch"] + "--")]
        doomed += [f"agents/{step['run']}/{t}" for t in step["tasks"]]
        for t in step["tasks"]:
            wt = Path(step["worktree_root"]).expanduser() / step["run"] / t
            if wt.exists():
                subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
        for b in doomed:
            subprocess.run(["git", "-C", str(repo), "branch", "-D", b], capture_output=True)
        return True, "deleted " + ", ".join(doomed)
    if kind == "mark_done":
        record_path = Path(step["path"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record_path.write_text(json.dumps({**record, "landed": True}, indent=2), encoding="utf-8")
        line = _mirror_landed(step)
        if line:
            print(line)
        stop = _close_approved_item(step.get("item"), runs_dir=record_path.resolve().parents[3], by=step.get("by", _UNLABELED),
                                    merged=True, mode=step.get("mode", "files"))
        if stop is not None:
            return False, stop
        return True, f"{step['task']} marked landed at {record_path}"
    if kind == "route_sync":
        # A failed sync is reported, never a failed land: after the merge nothing is undone,
        # before the PR it opens without `Closes`.
        when = "before the PR" if step.get("before") else "after the merge"
        tail = "the PR opens without Closes" if step.get("before") else "land not undone"
        sync = argparse.Namespace(item=step["item"], workspace=step["workspace"], dry_run=False, project=None,
                                  profile=step.get("profile"), runs_dir=step.get("runs_dir"))
        if _sync_skipped(sync):
            return True, f"sync of {step['item']} skipped {when}: tracker is none"
        try:
            rc = _route_sync(sync)
        except Exception as exc:
            return True, f"sync of {step['item']} failed {when} ({type(exc).__name__}: {exc}); {tail}"
        return True, f"synced {step['item']}" if rc == 0 else f"sync of {step['item']} failed {when} (exit {rc}); {tail}"
    return False, f"unknown step {kind!r}"


def _mark_done_facts(step: dict, pr: str, at: str) -> dict:
    """`step` with the store facts `mark_done` mirrors: run and phase from the
    `runs/<run>/tasks/<phase>/<task>.json` path, the PR url, and the caller's ISO time."""
    path = Path(step["path"])
    return {**step, "run": path.parents[2].name, "phase": path.parent.name, "pr": pr, "at": at}


def _mirror_landed(step: dict) -> str | None:
    """The line to print for the store mirror of a landed record, None when it took.
    A step with no `at` was not given the store facts and is not mirrored. Never raises."""
    if "at" not in step:
        return None
    try:
        # The record path is <runs_dir>/<run>/tasks/<phase>/<task>.json.
        result = store_cli.mark_landed(Path(step["path"]).resolve().parents[3], step["run"], step["phase"], step["task"], step["pr"], step["at"])
    except Exception as exc:
        return f"warning: store mirror of {step['task']} failed ({type(exc).__name__}: {exc}); land not undone"
    if isinstance(result, store_cli.Landed):
        return None
    if isinstance(result, store_cli.NotInStore):
        return f"store mirror of {step['task']} skipped: the record predates the mirror"
    if isinstance(result, store_cli.Failed):
        return f"warning: store mirror of {step['task']} failed (exit {result.code}: {result.detail}); land not undone"
    return f"warning: store mirror of {step['task']} skipped: harness not available; land not undone"


def _land_item_facts(item_path: str | None) -> tuple[str | None, str | None]:
    """`(id, issue)` from the work item's frontmatter, each `None` when the
    item is absent or does not carry it. `issue` is whatever the sync wrote
    back, normally a bare number."""
    text = _read_text_or_none(Path(item_path)) if item_path else None
    fields = route.parse_frontmatter(text)[0] if text is not None else {}
    item_id, issue = fields.get("id"), fields.get("issue")
    return (str(item_id) if item_id else None), (str(issue) if issue else None)


def _pr_url(detail: str) -> str | None:
    """The `pr_create` detail as the PR, only when it is a URL; the local forge opens none."""
    url = detail.strip()
    return url if url.startswith(("http://", "https://")) else None


def _landed_pr(kind: str, ok: bool, detail: str, prior: str) -> str:
    """The land's `pr` after a step: `""`, never None, is "no PR", because the store's `--pr` argv takes a str."""
    return (_pr_url(detail) or "") if kind == "pr_create" and ok else prior


def _resolved_tracker(profile: dict, runs_dir: Path) -> str:
    """The policy file wins, then the profile's `tracker`, then `none`."""
    return tracker.tracker_name(profile, runs_dir)


_UNLABELED = "unlabeled"


def _close_approved_item(item_path: str | None, *, runs_dir: Path, by: str, merged: bool = False, mode: str = "files") -> str | None:
    """Moves the item to `done` and mirrors any item left `done`, rerun or not, to the store; `merged` only after a merge step succeeded.

    Under `store` the store moves first, as `set-state done --expect approved`, whatever the local file says or even
    when it is missing; a refusal comes back as the reason, with the item untouched. Else None."""
    if item_path is None:
        return None
    item = Path(item_path)
    if mode == "store":
        stop = land.set_state_stop(store_cli.set_state(runs_dir, item.absolute().parents[1].name, item.stem, "done", by, expected="approved"))
        if stop is not None:
            return stop
    if not item.exists():
        return None
    text = item.read_text(encoding="utf-8")
    new_text, message = land.approve_to_done(text, merged=merged)
    if new_text is not None:
        item.write_text(new_text, encoding="utf-8")
    if message:
        print(message)
    if mode == "store" or (new_text is None and message is not None):
        return None
    task = str(route.parse_frontmatter(new_text or text)[0].get("id") or item.stem)
    line = store_cli.mirror_state(runs_dir, item.absolute().parents[1].name, task, "done", by)
    if line is not None:
        print(line)
    return None


_await_checks = land.await_checks
_wait_checks = forge_github.wait_checks


def _repo_is_dirty(repo: Path) -> bool:
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return bool(status.stdout.strip())


def sync_decision(has_origin: bool, behind: int, ahead: int) -> str:
    """Pure. What to do with the local default branch: skip, current, fast_forward or refuse."""
    if not has_origin:
        return "skip"
    if behind > 0 and ahead > 0:
        return "refuse"
    if behind > 0:
        return "fast_forward"
    return "current"


def _sync_default_branch(repo: Path, default_branch: str) -> tuple[str, str]:
    """Edge. Fast-forward the local default branch to origin's; HEAD and the tree stay put."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    remotes = git("remote").stdout.split()
    if "origin" not in remotes:
        return sync_decision(False, 0, 0), ""
    fetched = git("fetch", "origin", default_branch)
    if fetched.returncode != 0:
        return "refuse", fetched.stderr.strip()
    remote_ref = f"origin/{default_branch}"
    counted = git("rev-list", "--left-right", "--count", f"{default_branch}...{remote_ref}")
    if counted.returncode != 0:
        return "refuse", counted.stderr.strip()
    ahead, behind = (int(n) for n in counted.stdout.split())
    decision = sync_decision(True, behind, ahead)
    if decision == "refuse":
        return decision, f"{default_branch} has diverged from {remote_ref} ({ahead} ahead, {behind} behind); reconcile it first"
    if decision != "fast_forward":
        return decision, ""
    if git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() == default_branch:
        moved = git("merge", "--ff-only", remote_ref)
    else:
        old = git("rev-parse", f"refs/heads/{default_branch}").stdout.strip()
        moved = git("update-ref", f"refs/heads/{default_branch}", remote_ref, old)
    if moved.returncode != 0:
        return "refuse", (moved.stderr or moved.stdout).strip()
    return decision, f"{default_branch} fast-forwarded to {remote_ref} ({behind} commits)"


def _run_id_of(name: str) -> str:
    """`<id>.log`, `<id>.remote.json`, `<id>:<task>.json` and `<id>-trace` all belong to `<id>`."""
    return re.split(r"[.:]", name, maxsplit=1)[0].removesuffix("-trace")


def _taken_run_names(runs_dir: Path) -> list[str]:
    """Edge. Names in `runs_dir`, the bare run id each belongs to, and every run id in the store.

    A remote lane leaves only `<id>.remote.json`, which `next_run_id` does not read as an id; the bare ids let
    both it and an exact `--run-id` check see that run as taken."""
    names = [p.name for p in runs_dir.iterdir()] if runs_dir.is_dir() else []
    return [*names, *sorted({_run_id_of(n) for n in names}), *sorted(run_store.run_ids(runs_dir))]


def _runs_review(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    runs_dir = Path(profile["workspace_dir"]).expanduser() / "runs"
    run_id = route.next_run_id(_taken_run_names(runs_dir), "review")
    return review_pr.run_review(a.pr, profile, run_id, lambda argv: subprocess.run(argv, capture_output=True, text=True))


_LAND_LEASE_TTL_SECONDS = 3600
_GUARDED_STEPS = ("merge", "mark_done")


def _land_approval_stop(mode: str, store_state: str | None, file_state: str | None) -> str | None:
    """Pure. None when the state the approved check reads is `approved`, else the reason to stop, naming that state."""
    state = land.approved_state(mode, store_state, file_state)
    if state == "approved":
        return None
    if state == "done":
        return "land: task is already done; another machine landed it, so nothing is merged"
    return f"land: task state is {state!r}, not approved; nothing is merged"


def _land_store_stop(runs_dir: Path, record: dict, item_path: str | None) -> str | None:
    """Edge. The store's state and the item's, through the approved check; prints which of the two decided.

    `task_state_of` reads a missing row and a failed read alike as None, so the file line names both causes."""
    if item_path is None:
        return "land: refusing, the record names no initiative, so work_state store has no task to check or lease"
    initiative, task = record["initiative"], record["task"]
    store_state = run_store.task_state_of(runs_dir, initiative, task)
    text = _read_text_or_none(Path(item_path))
    file_state = route.parse_frontmatter(text)[0].get("state") if text else None
    source = "store" if store_state is not None else f"file (the store returned no row for {initiative}/{task}: none recorded, or the read failed)"
    print(f"land: state from {source}", file=sys.stderr)
    return _land_approval_stop("store", store_state, file_state)


def _lease_kept(runs_dir: Path, task: str, holder: str, epoch: int, kind: str) -> str | None:
    """Edge. None when the `land:<task>` lease renews at `epoch` before a guarded step, else the reason to stop."""
    if kind not in _GUARDED_STEPS:
        return None
    result = store_cli.lease_renew(runs_dir, store_cli.land_lease_name(task), holder, epoch, _LAND_LEASE_TTL_SECONDS)
    if isinstance(result, store_cli.LeaseGranted):
        return None
    lost = f"held by {result.holder} at epoch {result.epoch}" if isinstance(result, store_cli.LeaseRefused) else (
        result.detail if isinstance(result, store_cli.LeaseError) else "the store is not available")
    return f"land: refusing {kind}, the land lease for {task} could not be renewed: {lost}"


_LandWalk = Callable[[Callable[[str], str | None] | None], tuple[int, list[str], str]]


def _land_walked(a: argparse.Namespace, runs_dir: Path, mode: str, task: str | None, recheck: Callable[[], str | None],
                 walk: _LandWalk) -> tuple[int, list[str], str] | None:
    """`walk(None)` under `files`. Under `store` `recheck` runs again inside the `land:<task>` lease, and
    the walk renews the lease before `merge` and `mark_done`; the lease is released on every exit.

    None when the lease was refused or could not be taken, or the check under it stopped: the reason is printed and no step ran."""
    if mode != "store" or task is None:
        return walk(None)
    holder = chair.lease_holder(_holder_label(a), os.getpid(), socket.gethostname())

    def body(epoch: int) -> tuple[int, list[str], str] | str:
        stop = recheck()
        return stop if stop is not None else walk(lambda kind: _lease_kept(runs_dir, task, holder, epoch, kind))

    ran = land_lease.run_under_land_lease(runs_dir, task, holder, _LAND_LEASE_TTL_SECONDS, body)
    if isinstance(ran, land_lease.Refused):
        print(f"land: refusing, {task} is being landed by {ran.holder} (lease epoch {ran.epoch})")
        return None
    if isinstance(ran, land_lease.Unavailable):
        print(f"land: refusing, could not take the land lease for {task}: {ran.detail}")
        return None
    if ran.warning:
        print(ran.warning)
    if isinstance(ran.value, str):
        print(ran.value)
        return None
    return ran.value


def _land_done_before_fetch(runs_dir: Path, task: str | None, mode: str) -> bool:
    """Edge. True when work_state is store and the store already says `task` is done, so a fetch is pointless."""
    if task is None or mode != "store":
        return False
    initiative = _initiative_of(runs_dir.parent / "work", task)
    return initiative is not None and run_store.task_state_of(runs_dir, initiative, task) == "done"


def _runs_land(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"land: {reason}")
        return 2
    if a.task and _land_done_before_fetch(runs_dir, a.task, work_state.work_state_mode(_lake_provider(a)[0])):
        print(_land_approval_stop("store", "done", None))
        return 3
    # A remote run fetched before the marker existed has none, so land refuses with the fetch hint; a re-fetch is
    # idempotent (rsync -a and git fetch of the same refs).
    fetched = remote_lane.fetched_record_path(runs_dir, a.run_id).exists()
    if remote_lane.land_needs_fetch(remote_lane.remote_record_path(runs_dir, a.run_id).exists(), fetched):
        print(f"land: {a.run_id} is a remote run; run cox runs fetch {a.run_id}, then cox runs land {a.run_id} again")
        return 2
    if a.apply:
        guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim)
        if guard_rc is not None:
            return guard_rc
    profile_text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(profile_text) if profile_text is not None else {}
    except route.ProfileError as exc:
        print(f"land: profile unreadable: {exc}")
        return 2
    default_branch = "main"
    repo_facts = {"venv_python": (repo / ".venv" / "bin" / "python").exists(), "uv_lock": (repo / "uv.lock").exists()}
    phase = getattr(a, "phase", None) or (None if a.task else _phase_needing_land(runs_dir, a.run_id))
    land_mode = work_state.work_state_mode(_lake_provider(a)[0]) if a.apply else "files"
    record, item_path, lease_task = None, None, None
    if phase:
        phase_record, task_records, task_paths, searched = _land_phase_record(runs_dir, a.run_id, phase)
        if phase_record is None:
            print(f"land: no phase record at {searched}")
            return 2
        initiative = phase_record.get("initiative") or _initiative_of_phase(runs_dir.parent / "work", phase)
        items, items_path = (_phase_items(runs_dir.parent / "work", initiative, phase)
                              if initiative else (None, f"{runs_dir.parent / 'work'} (no single initiative holds phase {phase})"))
        if items is None:
            print(f"land: no work items at {items_path}, expected the phase's tickets")
            return 2
        plan_steps = land.land_plan({**phase_record, "initiative": initiative}, {}, default_branch, repo_facts,
                                    items=items, task_records=task_records)
        steps = _land_enrich(plan_steps, path=searched, worktree_root=a.worktree_root, task_paths=task_paths,
                              umbrella=profile.get("umbrella_dir"))
        lease_task = f"phase:{initiative}/{phase}"
    else:
        record, searched, count, source = _land_load(runs_dir, a.run_id, a.task)
        if record is None:
            print(f"land: looked in {searched}, found {count} task records, expected 1")
            return 2
        print(f"land: record from {source}", file=sys.stderr)
        lease_task = record["task"]
        branches = _land_branches(repo, record, default_branch)
        item_path = (str(runs_dir.parent / "work" / record["initiative"] / record["phase"] / f"{record['task']}.md")
                     if record.get("initiative") else None)
        item_id, issue = _land_item_facts(item_path)
        plan_steps = land.land_plan(record, branches, default_branch, repo_facts,
                                    tracker=_resolved_tracker(profile, runs_dir), issue=issue)
        steps = _land_enrich(plan_steps, path=searched, worktree_root=a.worktree_root, item_path=item_path,
                              workspace=str(runs_dir.parent), item_id=item_id,
                              profile=str(_profile_path(a)), runs_dir=str(runs_dir), umbrella=profile.get("umbrella_dir"))
    level = a.gate or _resolved_gate_level(runs_dir)
    if a.gate:
        print(f"land: --gate {level} overrides the resolved level")
    planned, steps = steps, land.gate_steps(steps, level)
    if not a.apply:
        print(json.dumps(steps, indent=2))
        return 2 if any(s["kind"] == "refuse" for s in steps) else 0
    forge_choice = forge.forge_name(profile)
    forge_module = forge.forge_for(forge_choice)
    if forge_module is None:
        print(f"land: no forge named {forge_choice} (built in: local, github)")
        return 2
    stale = forge.missing_refs(forge_module)
    if stale:
        print(f"land: refusing, forge {forge_choice} does not accept {', '.join(stale)}; see agent_tools/forge.py")
        return 2
    print(f"forge: {forge_choice}")
    if land_mode == "store" and record is not None:
        stop = _land_store_stop(runs_dir, record, item_path)
        if stop is not None:
            print(stop)
            return 2
    elif land_mode == "store":
        print(f"land: work_state store leases the phase as {lease_task}; its items are already done, so there is no approved check")
    if _repo_is_dirty(repo):
        print(f"land: refusing, {repo} is dirty")
        return 2
    sync, sync_detail = _sync_default_branch(repo, default_branch)
    if sync == "fast_forward":
        print(f"land: {sync_detail}")
    elif sync == "refuse":
        print(f"land: refusing, {sync_detail}")
        return 2
    cherry_pick = next((s for s in steps if s["kind"] == "cherry_pick"), None)
    if cherry_pick is not None:
        # An existing pr/<task> is not necessarily stale or foreign: a retried
        # push can leave a same-tree branch behind, and the rerun should reuse it.
        decision = _land_resume(repo, cherry_pick, forge_module)
        if decision["kind"] == "refuse":
            print(f"land: refusing, branch {cherry_pick['onto']} already exists in {repo}: {decision['reason']}")
            return 2
        if decision["kind"] == "resume":
            print(f"land: resuming on existing branch {cherry_pick['onto']}")
        steps = land.resume_steps(steps, decision, cherry_pick["onto"])
        planned = land.resume_steps(planned, decision, cherry_pick["onto"])
    # Steps start here: every return from now on is logged. Earlier returns are not.
    def walk(guard):
        return _land_execute(repo, steps, planned, record, item_path, level, a.no_merge, forge_module, by=_holder_label(a),
                             mode=land_mode, guard=guard)

    repo_holder = f"{_holder_label(a)}@{socket.gethostname()}:{os.getpid()}"
    got = land_repo_lease.acquire(runs_dir, str(repo), repo_holder, 1200)
    if isinstance(got, store_cli.LeaseRefused):
        print(land_repo_lease.refusal_message(str(repo), got.holder or "another land"))
        walked = None
    elif isinstance(got, store_cli.LeaseGranted):
        try:
            walked = _land_walked(a, runs_dir, land_mode, lease_task,
                                   lambda: _land_store_stop(runs_dir, record, item_path) if record else None, walk)
        finally:
            land_repo_lease.release(runs_dir, str(repo), repo_holder, got.epoch)
    else:
        # A store outage (NotAvailable/LeaseError) must not block a land: walk without the repo lease.
        walked = _land_walked(a, runs_dir, land_mode, lease_task,
                               lambda: _land_store_stop(runs_dir, record, item_path) if record else None, walk)
    rc, reached, pr = walked if walked is not None else (2, [], "")  # a refused lease stops before any step, and is logged too
    task = record["task"] if record else None
    _append_land_log(runs_dir, land.land_log_row(datetime.datetime.now(datetime.UTC).isoformat(), a.run_id, task, reached, rc, pr))
    _lake_after_land(a, runs_dir, rc, reached)
    return rc


def _append_land_log(runs_dir: Path, row: dict) -> None:
    with (runs_dir / "land.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def should_sync_lake(rc: int, reached: Sequence[str], extra_imports: bool, config_resolved: bool,
                     catalog_path: Path | None, catalog_exists: bool) -> bool:
    """A land syncs the lake when it exited 0 having reached `mark_done`, the extra imports and the config resolved.

    `catalog_path` is the SQLite catalog file, None for any other catalog; that file must already exist."""
    return (rc == 0 and "mark_done" in reached and extra_imports and config_resolved
            and (catalog_path is None or catalog_exists))


def _lake_after_land(a: argparse.Namespace, runs_dir: Path, rc: int, reached: Sequence[str]) -> None:
    """Edge. Sync a lake that already exists once a land has closed its item. Never changes the land's exit code."""
    try:
        # Imported here, as `_lake_doctor` does: the lake modules stay off cli.py's import path.
        from agent_tools import lake_doctor

        runs = runs_dir.resolve()
        provider, problem = _lake_provider(a)
        config = lake_config.resolve_lake(provider, runs)
        catalog_path = lake_doctor.sqlite_catalog_path(config.catalog_uri)
        extra = all(importlib.util.find_spec(name) is not None for name in ("pyiceberg", "pyarrow"))
        exists = catalog_path is not None and catalog_path.exists()
        if not should_sync_lake(rc, reached, extra, problem is None, catalog_path, exists):
            return
        root = store_url.profile_traces_root(provider, runs).url
        root = root if "://" in root else str(Path(root).resolve())
        sync = _lake_real_run(config, store_url.profile_store_url(provider, runs), root)
        outcome = lake_lease.sync_under_lease(sync, runs)
        if isinstance(outcome, lake_lease.Synced):
            report = outcome.value
            rows = sum(t["rows_appended"] for t in report["tables"])
            print(f"lake: +{rows} rows, +{report['traces']['registered']} trace files")
        elif isinstance(outcome, lake_lease.SkippedHeld):
            print(f"lake: sync skipped (held by {outcome.holder})")
        else:
            print(f"lake: sync skipped ({outcome.name})")
    except Exception as exc:  # the land has already happened; a lake problem must not fail it
        print(f"lake: sync skipped ({type(exc).__name__})")


def _land_execute(repo: Path, steps: list[dict], planned: list[dict], record: dict | None, item_path: str | None,
                  level: str, no_merge: bool, forge_module=forge_github, by: str = _UNLABELED,
                  mode: str = "files", guard: Callable[[str], str | None] | None = None) -> tuple[int, list[str], str]:
    """Walk `steps`; return the exit code, the step kinds run in order, and the PR url ("" when none opened).

    A pr branch's land worktree is dropped before `merge` and on every stop; the branch itself stays.
    `guard(kind)`, when given, runs before each step; a reason back stops the land with exit 2 before that step."""
    built: list[str] = []
    try:
        return _land_walk(repo, steps, planned, record, item_path, level, no_merge, forge_module, built, by=by, mode=mode,
                          guard=guard)
    finally:
        for branch in built:
            _remove_land_worktree(repo, branch)


def _land_walk(repo: Path, steps: list[dict], planned: list[dict], record: dict | None, item_path: str | None,
               level: str, no_merge: bool, forge_module, built: list[str], by: str = _UNLABELED,
               mode: str = "files", guard: Callable[[str], str | None] | None = None) -> tuple[int, list[str], str]:
    pr = ""
    reached: list[str] = []
    for i in range(len(steps)):
        step = steps[i]
        if step["kind"] == "refuse":
            print(f"refused: {step['reason']}")
            return 2, reached, pr
        if step["kind"] == "note":
            continue
        held = guard(step["kind"]) if guard is not None else None
        if held is not None:
            print(held)
            print("stopped; remaining: " + ", ".join(s["kind"] for s in steps[i:]))
            return 2, reached, pr
        reached.append(step["kind"])
        if step["kind"] in ("cherry_pick", "reuse_branch"):
            built.append(step.get("onto") or step["branch"])
        if step["kind"] == "merge":
            # gh and git cannot delete a branch a worktree still holds.
            for branch in built:
                _remove_land_worktree(repo, branch)
        if step["kind"] == "mark_done":
            if record is not None and not Path(step["path"]).exists():
                # The store copy is written out as the record file, so the path-derived run, phase and runs_dir below keep working.
                Path(step["path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(step["path"]).write_text(json.dumps(record, indent=2), encoding="utf-8")
            step = {**_mark_done_facts(step, pr, datetime.datetime.now(datetime.UTC).isoformat()), "by": by, "mode": mode}
        ok, detail = _execute_land_step(repo, step, forge_module)
        if step.get("before") == "pr_create":
            # The sync may have just written `issue:`; the PR opened next must carry its `Closes`.
            steps = land.with_issue(steps, record, _land_item_facts(item_path)[1])
        pr = _landed_pr(step["kind"], ok, detail, pr)
        if step["kind"] == "checks" and not ok and detail.startswith(_LAUNCH_ERROR):
            # A check whose executable `subprocess` can't find is a refusal,
            # not an ordinary failure, and it fires before `push` so a check
            # that never ran leaves no pushed branch behind.
            print(detail)
            return 2, reached, pr
        print(f"{step['kind']}: {detail}")
        if not ok and step["kind"] == "cherry_pick" and detail.startswith(_GENERATE_FAILED):
            # `_execute_land_step` detached it and deleted its branch; the next land's cherry_pick removes it.
            built.remove(step["onto"])
            print(f"land: worktree left at {_land_worktree(repo, step['onto'])} for inspection")
        if not ok:
            remaining = [s["kind"] for s in steps[i + 1:]]
            print("stopped; remaining: " + ", ".join(remaining))
            return 1, reached, pr
        if step["kind"] == "wait_checks" and no_merge:
            print("stopping after wait_checks (--no-merge)")
            return 0, reached, pr
    stop = land.gate_stop(planned, steps, level, pr)
    if stop:
        print(stop)
    return (3 if stop else 0), reached, pr


def _runs_recover(a: argparse.Namespace) -> int:
    """Merge one approved task's commit into its phase branch, the remedy for
    a merge the harness escalated to `self_modification` and so never
    applied. Never runs the repo's checks, never pushes: this only lands one
    commit on a local phase branch, and the gate to `main` stays a person's."""
    repo = Path(a.repo).expanduser()
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"recover: {reason}")
        return 2
    record, searched, count = _land_record(runs_dir, a.run_id, a.task_id)
    if record is None:
        print(f"recover: looked in {searched}, found {count} task records, expected 1")
        return 2
    work_root = runs_dir.parent / "work"
    initiative = _initiative_of(work_root, a.task_id)
    if initiative is None:
        print(f"recover: {a.task_id}: no single initiative under {work_root} holds this task")
        return 2
    record = land.recover_record(searched, record.get("ticket"), initiative)
    if record.get("kind") == "refuse":
        print(f"recover: {record['reason']}")
        return 2
    phase = record["phase"]
    phase_branch = f"epic/{initiative}/{phase}"
    if not _branch_exists(repo, phase_branch):
        print(f"recover: phase branch {phase_branch} does not exist in {repo}")
        return 2
    item_path = str(work_root / initiative / phase / f"{a.task_id}.md")
    mark_done_step = {"kind": "mark_done", "item": item_path, "from": "approved", "to": "done"}
    branches = _recover_branches(repo, record, phase_branch)
    step = land.recover_plan(record, branches)[0]
    if step["kind"] == "already_recovered":
        print(f"recover: {step['branch']} already contains the commit ({step['reason']}); nothing to do")
        if a.dry_run:
            print(json.dumps(mark_done_step))
            return 0
        _close_approved_item(item_path, runs_dir=runs_dir, by=_holder_label(a))
        return 0
    if step["kind"] == "refuse":
        print(f"refused: {step['reason']}")
        return 2
    source, target, subject = step["source"], step["target"], step["commit_subject"]
    message = f"Merge branch '{source}' into {target}"
    if a.dry_run:
        print(f"recover: would merge {source} into {target} --no-ff -m {message!r} ({subject!r})")
        print(json.dumps(mark_done_step))
        return 0
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", target], capture_output=True, text=True).stdout.strip()
    wt = Path(tempfile.mkdtemp(prefix="recover-"))
    add = subprocess.run(["git", "-C", str(repo), "worktree", "add", str(wt), target], capture_output=True, text=True)
    if add.returncode != 0:
        print(f"refused: could not check out {target}: {add.stderr.strip() or add.stdout.strip()}")
        return 2
    try:
        merge = subprocess.run(["git", "-C", str(wt), "merge", "--no-ff", source, "-m", message], capture_output=True, text=True)
        if merge.returncode != 0:
            subprocess.run(["git", "-C", str(wt), "merge", "--abort"], capture_output=True, text=True)
            print(f"refused: merge of {source} into {target} was not clean: {merge.stderr.strip() or merge.stdout.strip()}")
            return 2
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], capture_output=True)
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", target], capture_output=True, text=True).stdout.strip()
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--stat", before, after], capture_output=True, text=True)
    print(f"recover: {target} {before[:8]} -> {after[:8]}")
    print(diff.stdout.strip())
    _close_approved_item(item_path, runs_dir=runs_dir, by=_holder_label(a))
    return 0


def _lane_live(runs_dir: Path, run: str) -> bool:
    pidfile = runs_dir / f"{run}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        pid = None
    return epic.run_live(pid, pidfile)


def _lane_outcome(runs_dir: Path, run: str, at: str) -> dict:
    try:
        log = (runs_dir / f"{run}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    s = epic.summarize_log(log)
    return {"run": run, "at": at, "approved": s["approved"], "quarantined": s["quarantined"], "summary": s["summary"]}


def _runs_wait(a: argparse.Namespace) -> int:
    d = Path(a.runs_dir)
    busy = [pf.stem for pf in sorted(d.glob("*.pid")) if _lane_live(d, pf.stem)]
    if not busy:
        print("all lanes clear")
        return 2
    exited, still = epic.wait_lanes(busy, lambda r: _lane_live(d, r), time.monotonic, time.sleep, a.max_seconds, a.interval)
    at = datetime.datetime.now(datetime.UTC).astimezone().strftime("%H:%M")  # when the exit was noticed, not when it happened
    outcomes = [_lane_outcome(d, r, at) for r in exited]
    if a.json:
        print(json.dumps({"exited": outcomes, "busy": still}, indent=2))
    elif not outcomes:
        print(f"still busy: {' '.join(still)}")
    else:
        for o in outcomes:
            print(f"exited {o['run']} at {o['at']}")
            print("\n".join([*o["approved"], *o["quarantined"], *([o["summary"]] if o["summary"] else [])]))
    return 0 if outcomes else 3


def _epic_watch(a: argparse.Namespace) -> int:
    out = epic.watch(a.pidfile, log=a.log, max_seconds=a.max_seconds, interval=a.interval)
    print(json.dumps(out, indent=2) if a.json else "\n".join(
        [f"pid {out['pid']}: {'finished' if out['finished'] else 'still running'}",
         *out.get("quarantined", []), *out.get("reused", []), *(x for x in (out.get("summary"), out.get("usage")) if x)]))
    return 0 if out["finished"] else 3






def _plan_serve(a: argparse.Namespace) -> int:
    if a.check:
        print(plan.check(a.dir))
    print(plan.serve(a.dir, kind=a.kind, open_browser=not a.no_open)); return 0


DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"


def _profile_path(a: argparse.Namespace) -> Path:
    return Path(a.profile or os.environ.get("AGENT_TOOLS_PROFILE") or DEFAULT_PROFILE).expanduser()


def _read_text_or_none(p: Path):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _mtime_iso(p: Path):
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).isoformat()


def _heartbeats(runs_dir: Path, run_ids) -> dict:
    """Edge. `run id -> heartbeat_at` for each run that holds its own store lease."""
    leases = {run_id: run_store.lease(runs_dir, run_id) for run_id in run_ids}
    return {run_id: row[2] for run_id, row in leases.items() if row is not None and row[0] == run_id}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _with_remote_lanes(runs_dir: Path, local_entries: list) -> list:
    """Edge. The local run entries, then an entry for each live store lease no local pidfile names.

    `now` must be `_now_iso()`'s form: `live_lanes` compares lease times as text."""
    lanes = run_store.live_lanes(runs_dir, _now_iso())
    remote = run_store.remote_lanes(lanes, {entry["id"] for entry in local_entries})
    return [*local_entries, *route.remote_lane_entries(remote)]


def _with_lane_fields(rows: list, runs: list, now: str) -> list:
    """`status_rows` output with `host` and `remote` on every row. A remote row reads `alive` and carries its lane text as `summary`, because `status_rows` has no place for either."""
    by_id = {run["id"]: run for run in runs}

    def one(row: dict) -> dict:
        run = by_id.get(row["id"], {})
        if not run.get("remote"):
            return {**row, "host": run.get("host"), "remote": False}
        lane = route._lane(run, now)
        return {**row, "state": "alive", "host": run["host"], "remote": True, "summary": lane.removeprefix(run["id"] + " ")}

    return [one(row) for row in rows]


def _efficiency(runs_dir: Path, now: str) -> str | None:
    """Edge. The `efficiency:` line for the UTC day of `now`; None with no store or one that fails to answer."""
    today = now[:10]
    tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
    try:
        spend = run_store.cost_since(runs_dir, today, tomorrow)
        if spend is None:
            return None
        landed = route.landed_today(_read_text_or_none(runs_dir / "land.jsonl") or "", today)
        counts = run_store.build_counts(runs_dir, [t for _, t in landed if t is not None], [r for r, _ in landed if r is not None])
    except (*run_store._DB_ERRORS, OSError, RuntimeError, ValueError, TypeError):
        return None
    rate, measured = route.first_try(landed, counts)
    return route.efficiency_line(spend, len(landed), rate, measured)


def _gather_context(profile_path: Path, mode: str = "files"):
    """Read the profile and the workspace; return (profile_or_none, reason, intake, runs, initiatives, problems, efficiency)."""
    text = _read_text_or_none(profile_path)
    if text is None:
        return None, f"no profile at {profile_path}", [], [], [], [], None
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return None, f"profile unreadable: {exc}", [], [], [], [], None
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        return profile, "workspace_dir not set in profile", [], [], [], [], None
    ws = Path(workspace).expanduser()
    pid_paths = sorted((ws / "runs").glob("*.pid"))
    pids = {p.stem: t for p in pid_paths if (t := _read_text_or_none(p)) is not None}
    alive = {rid: epic.run_live(route.parse_pid(t), ws / "runs" / f"{rid}.pid") for rid, t in pids.items()}
    started = {rid: _mtime_iso(ws / "runs" / f"{rid}.pid") for rid in pids}
    # the initiative id is the DIRECTORY name: work/<initiative>/<phase>/<task>.md;
    # the edge only reads and names the path parts — route.work_item normalises
    items = _stored_work_items(ws, mode)
    return (profile, "",
            _intake_groups(ws, items),
            _with_remote_lanes(ws / "runs", route.run_entries(pids, alive, started, _heartbeats(ws / "runs", pids))),
            route.initiative_summaries(items),
            route.state_problems(items),
            _efficiency(ws / "runs", _now_iso()))


def _route_context(a: argparse.Namespace) -> int:
    # The edge is allowed to catch everything _gather_context can raise: this
    # command must never take a session down with it (spec §2). That
    # tolerance is for profile/workspace file reads only — it stops at the
    # gatherer, per charter A6, so a bug in the usage assessment surfaces
    # instead of erasing an otherwise-good docket (run tools-pacing-7).
    try:
        profile, reason, intake, runs, initiatives, problems, efficiency = _gather_context(_profile_path(a), work_state.work_state_mode(_lake_provider(a)[0]))
    except Exception as exc:
        print(f"routing: context unavailable ({type(exc).__name__}: {exc})")
        return 0
    # Computed once via the gatherer, same reason `route launch` gates on;
    # only when there is a workspace to gather usage files from.
    usage_reason = (
        _usage_assessment(
            Path(profile["workspace_dir"]).expanduser() / "runs",
            profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
        ).reason
        if not reason and profile is not None else None
    )
    if a.json:
        doc = route.context_document(profile, intake, runs, initiatives, problems)
        if reason:
            doc["reason"] = reason
        if usage_reason is not None:
            doc["usage"] = usage_reason
        print(json.dumps(doc, indent=2))
    elif reason:
        # nothing was read, so print no counts: an unread workspace must not
        # look like an empty one
        empty_groups = {"queued": [], "decomposed": [], "landed": []}
        first_line = route.render_context(profile, empty_groups, [], []).partition("\n")[0]
        print(f"{first_line} ({reason})")
    else:
        gate_level = _resolved_gate_level(Path(profile["workspace_dir"]).expanduser() / "runs")
        rendered = route.render_context(profile, intake, runs, initiatives, problems, gate_level=gate_level, now=_now_iso(), efficiency=efficiency)
        print(f"{rendered}\nusage: {usage_reason}")
    return 0


def _refuse_if_already_running(runs_dir: Path, prefix: str):
    """spec: at most one live run per `prefix` — the single-writer rule
    applied to a run-id prefix. Returns the refusal line to print, or
    None when no pidfile under `prefix` names a live pid."""
    live_pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)\.pid$")
    for pidfile in sorted(runs_dir.glob(f"{prefix}-*.pid")):
        if not live_pattern.fullmatch(pidfile.name):
            continue
        pid_text = _read_text_or_none(pidfile)
        pid = route.parse_pid(pid_text) if pid_text is not None else None
        if epic.run_live(pid, pidfile):
            return f"routing: {pidfile.stem} is already running (pid {pid})"
    return None


def _work_items(ws: Path) -> list:
    return [
        route.work_item(fields, initiative=task_path.parent.parent.name, phase_dir=task_path.parent.name, stem=task_path.stem)
        for task_path in sorted((ws / "work").glob("*/*/*.md"))
        if task_path.name != "initiative.md"
        for text in [_read_text_or_none(task_path)] if text is not None
        for fields in [route.parse_frontmatter(text)[0]]
    ]


def _stored_work_items(ws: Path, mode: str) -> list:
    """Edge. `_work_items` with each state from the store under mode "store"; the store is not read under "files"."""
    items = _work_items(ws)
    return route.with_store_states(items, run_store.work_items(ws / "runs") if mode == "store" else [], mode)


def _initiative_texts(ws: Path) -> dict:
    return {
        p.name: _read_text_or_none(p / "initiative.md") or ""
        for p in sorted((ws / "work").glob("*"))
        if (p / "initiative.md").is_file()
    }


def _intake_groups(ws: Path, items: list) -> dict:
    intake_root = ws / "intake"
    paths = sorted(intake_root.glob("*.md")) + sorted((intake_root / "done").glob("*.md"))
    files = {str(p.relative_to(intake_root)): t for p in paths if (t := _read_text_or_none(p)) is not None}
    texts = _initiative_texts(ws)
    states = route.initiative_states(sorted(texts), items)
    initiatives = [{"id": iid, "done": states[iid], "text": texts[iid]} for iid in texts]
    return route.intake_groups(route.intake_entries(files), initiatives)


def _intake_groups_for(ws: Path, items: list):
    """`_intake_groups` from disk, or `None` with no `intake/` dir."""
    return _intake_groups(ws, items) if (ws / "intake").is_dir() else None


def _status_rows_for(runs_dir: Path) -> list:
    pids = {p.stem: t for p in sorted(runs_dir.glob("*.pid")) if (t := _read_text_or_none(p)) is not None}
    alive = {run_id: epic.run_live(route.parse_pid(t), runs_dir / f"{run_id}.pid") for run_id, t in pids.items()}
    started = {run_id: _mtime_iso(runs_dir / f"{run_id}.pid") for run_id in pids}
    runs = _with_remote_lanes(runs_dir, route.run_entries(pids, alive, started, _heartbeats(runs_dir, pids)))
    summaries = {p.stem: epic.summarize_log(_read_text_or_none(p) or "") for p in runs_dir.glob("*.log")}
    return _with_lane_fields(route.status_rows(route.status_entries(runs, summaries)), runs, _now_iso())


def _route_drift(a: argparse.Namespace) -> int:
    """Report only: exits 0 whether or not the store and the files disagree."""
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    ws = Path(workspace).expanduser()
    rows = run_store.work_items(ws / "runs")
    if not rows:
        print("store has no work_items")
        return 0
    files = [(item["initiative"], item["id"], item["state"]) for item in _work_items(ws)]
    found = route_drift.drift(files, rows)
    print(route_drift.format_json(found) if a.json else route_drift.format_text(found))
    return 0


def _route_import_files(ws: Path) -> list[tuple[tuple[str, ...], str]]:
    """Edge: `(path_parts, text)` for every intake and task file under `ws`, `path_parts` relative to `ws`."""
    paths = sorted(ws.glob("intake/*.md")) + sorted(ws.glob("intake/done/*.md")) + sorted(ws.glob("work/*/*/*.md"))
    return [(p.relative_to(ws).parts, t) for p in paths if (t := _read_text_or_none(p)) is not None]


def _route_import_workspace(a: argparse.Namespace) -> Path | str:
    """`--workspace`, else the profile's `workspace_dir`, else `.`, as `_sync_context` resolves it.
    A str is why the profile is unreadable."""
    if a.workspace:
        return Path(a.workspace).expanduser()
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError as exc:
        return f"profile unreadable: {exc}"
    return Path(profile.get("workspace_dir") or ".").expanduser()


def _route_import(a: argparse.Namespace) -> int:
    """Loads intake/ and work/<initiative>/<phase>/*.md files into the store's work_items table.
    Idempotent: importing the same files again reports zero written. Exits 2, after printing the
    failing row's key and the store's error, then the count written so far, the moment a row fails
    to upsert."""
    ws = _route_import_workspace(a)
    if isinstance(ws, str):
        print(f"route import: {ws}")
        return 2
    # A workspace with neither dir is a wrong path, not an empty queue: refuse rather than report "written 0".
    if not (ws / "work").is_dir() and not (ws / "intake").is_dir():
        print(f"route import: no work/ or intake/ under {ws}; pass --workspace or set workspace_dir in the profile")
        return 2
    runs_dir = ws / "runs"
    plan = route_import.plan_import(_route_import_files(ws), run_store.read_queue(runs_dir))
    written = 0
    for row in plan.to_write:
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            print(f"route import: store refused {row['initiative']}/{row['task_id']}: {detail}")
            print(route_import.format_summary(written, plan))
            return 2
        written += 1
    print(route_import.format_summary(written, plan))
    return 0


def _route_draft(a: argparse.Namespace, act: Callable[[Path, draft_apply.Store], int]) -> int:
    """Resolve the workspace and the work-state mode, then hand both to `act`."""
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"route: {reason}")
        return 2
    store = draft_apply.Store(
        work_state.work_state_mode(_lake_provider(a)[0]),
        lambda initiative: run_store.work_items(runs_dir, initiative),
        lambda initiative, task, state, by, expected: store_cli.set_state(runs_dir, initiative, task, state, by, expected),
    )
    return act(runs_dir.parent / "work", store)


def _route_approve(a: argparse.Namespace) -> int:
    return _route_draft(a, lambda work, store: draft_apply.approve(work, a.initiative, a.task, a.by, store))


def _route_decline(a: argparse.Namespace) -> int:
    return _route_draft(a, lambda work, store: draft_apply.decline(work, a.initiative, a.reason, a.by, store))


def _route_status(a: argparse.Namespace) -> int:
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    # Past this point a stray pidfile — hand-edited, absurdly large — must
    # not take the session down with it (spec §2); charter A6 puts
    # exceptions at the edge, matching _route_context's guard above.
    try:
        ws = Path(workspace).expanduser()
        rows = _status_rows_for(ws / "runs")
        items = _stored_work_items(ws, work_state.work_state_mode(_lake_provider(a)[0]))
        groups = _intake_groups_for(ws, items)
        problems = route.state_problems(items)
        if a.json:
            # The bare rows list is the shape older callers read; it stays when there
            # is nothing else to say. A problem is never dropped for lack of an intake dir.
            if groups is None and not problems:
                doc = rows
            elif groups is None:
                doc = {"runs": rows, "problems": problems}
            else:
                doc = {"runs": rows, "intake": groups, "problems": problems}
            print(json.dumps(doc, indent=2))
        else:
            shown, hidden = (rows, 0) if a.all else route.recent_rows(rows, datetime.datetime.now(datetime.UTC))
            status = route.render_status(shown, groups, problems, gate_level=_resolved_gate_level(ws / "runs"), hidden=hidden)
            drafts = route.render_drafts(
                draft_list.read_drafts(ws / "work", datetime.datetime.now(datetime.UTC).isoformat()), draft_list.format_age
            )
            print("\n".join(part for part in (status, drafts) if part))
    except Exception as exc:
        print(f"routing: status unavailable ({type(exc).__name__}: {exc})")
    return 0


def _leader_runs_dir_or_refuse(a: argparse.Namespace):
    """spec: the leader lock lives under the profile's workspace, same as
    `route status`'s runs dir. Returns (profile, runs_dir, None) or
    (None, None, 2) after printing the refusal."""
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return None, None, 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return None, None, 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return None, None, 2
    return profile, Path(workspace).expanduser() / "runs", None


def _leader_heartbeat_minutes() -> int:
    """`policy.leader.heartbeat_minutes` from the resolved cartridge dict, default 10.
    Resolving a cartridge's own policy is another repository's item, and
    `route.parse_profile`'s flat `key: scalar` schema (spec §1) has no `policy` key to
    read in the meantime, so this always answers the default until that lands."""
    return chair.DEFAULT_HEARTBEAT_MINUTES


def _leader_takeover_hours() -> float:
    """`policy.leader.takeover_hours`, default 3; answers the default for the same reason `_leader_heartbeat_minutes` does."""
    return 3.0


def _leader_refuse_hours(hours: float | None) -> int | None:
    if hours is None or hours > 0:
        return None
    print(f"chair: --hours must be positive, got {hours}")
    return 2


def _leader_read_or_refuse(runs_dir: Path):
    """A file present but unreadable or not valid JSON is a refusal, not a free lock
    (charter A6: `chair.read` raises at the edge; here is where that becomes a value).
    Returns `(record_or_none, None)` or `(None, 2)` after printing the reason."""
    try:
        return chair.read(runs_dir), None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"chair: lock file unreadable ({type(exc).__name__}: {exc})")
        return None, 2


def _holder_label(a: argparse.Namespace) -> str:
    return getattr(a, "label", None) or os.environ.get("COX_SESSION_LABEL") or _UNLABELED


def _leader_guard_or_refuse(runs_dir: Path, holder: str, force: bool, claim: bool = False) -> int | None:
    """Exit code 2 with the refusal printed when another live session holds the loop;
    None to proceed. `claim` takes an unheld, stale or crashed lock for `holder` before
    proceeding, so launching or landing is what makes a session the leader."""
    record, rc = _leader_read_or_refuse(runs_dir)
    if rc is not None:
        return None
    state = chair.liveness(record, _leader_pid_alive(record), datetime.datetime.now(datetime.UTC), socket.gethostname(), _leader_heartbeat_minutes())
    line = chair.guard(record, holder, state)
    if line is not None:
        print(f"override: {line}" if force else line)
        return None if force else 2
    if claim and state != "live":
        pid, host = _leader_identity()
        new_record, _ = chair.take(record, holder, pid, host, datetime.datetime.now(datetime.UTC), _leader_heartbeat_minutes(), _leader_pid_alive(record), steal=True, claude_session=chair.claude_session_from_env(os.environ))
        chair.write(runs_dir, new_record)
        print(f"taking the loop: {holder}" if record is None else f"taking the loop from {record.get('session')} ({state})")
    return None


def _leader_identity(explicit_pid: int | None = None) -> tuple[int, str]:
    """The pid and host that name *this session* to the lock, for `take`/`beat`/`release`.
    `os.getpid()` names this one `cox` invocation, which exits the moment the command
    returns. `os.getppid()` names the process that invoked `cox`, which is the right
    answer only when a single long-running shell issues `take`, `beat` and `release` in
    turn; a caller that runs each command in a fresh shell records a pid that is dead
    before the next command, so its lock reads as stale the instant it is taken.
    `--pid` lets such a caller name the durable process that actually owns the loop."""
    return explicit_pid or os.getppid(), socket.gethostname()


def _leader_pid_alive(record: dict | None) -> bool:
    """A pid is judged dead only on the host that recorded it: this process has no way
    to check a pid number on another machine, so a foreign host's lock is never read as
    dead here — only its own host, or its heartbeat aging out, can make it stale."""
    if record is None:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    if record.get("host") != socket.gethostname():
        return True
    return chair.pid_alive(pid)


def _leader_refuse_dead_pid(explicit_pid: int | None) -> int | None:
    """Refuses only an explicit `--pid`; the `os.getppid()` default is alive by construction."""
    if explicit_pid is None or chair.pid_alive(explicit_pid):
        return None
    print(f"chair pid not alive: {explicit_pid}")
    return 2


def _leader_launched_by(record: dict | None, pid_alive_: bool, now: datetime.datetime, host: str, heartbeat_minutes: int) -> str | None:
    """The label to stamp on a run launched right now: the lock's own holder when it
    reads live, `None` when it reads stale, crashed, or none — never a guess."""
    if chair.liveness(record, pid_alive_, now, host, heartbeat_minutes) != "live":
        return None
    return record.get("session")


def _print_if_stale(record: dict | None, state: str) -> None:
    if record is not None and state in ("stale", "crashed"):
        print(f"chair {state}: {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')}")


def _route_chair_take(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    session = a.label or "chair"
    pid, host = _leader_identity(a.pid)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    hours = getattr(a, "hours", None)  # the bare launcher's namespace has none: its record carries no `until`
    refuse_rc = _leader_refuse_hours(hours)
    if refuse_rc is not None:
        return refuse_rc
    heartbeat_minutes = _leader_heartbeat_minutes()
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        now = datetime.datetime.now(datetime.UTC)
        alive = _leader_pid_alive(record)
        prior_state = chair.liveness(record, alive, now, host, heartbeat_minutes)
        _print_if_stale(record, prior_state)
        new_record, reason = chair.take(record, session, pid, host, now, heartbeat_minutes, alive, steal=a.steal, claude_session=chair.claude_session_from_env(os.environ), hours=hours)
        if new_record is None and a.steal and prior_state == "live" and chair.same_holder(record, session, pid, host):
            refusal = chair.acquire_lease(runs_dir, session, pid, host, steal=True)
            if refusal:
                print(refusal)
                return 2
            chair.write(runs_dir, {**record, "heartbeat_at": now.isoformat()})
            print(f"chair lease retaken: {session} (pid {pid}) on {host}")
            return 0
        if new_record is None:
            print(reason)
            return 2
        refusal = chair.acquire_lease(runs_dir, session, pid, host, steal=a.steal)
        if refusal:
            print(refusal)
            return 2
        chair.write(runs_dir, new_record)
    print(f"chair taken: {new_record['session']} (pid {new_record['pid']}) on {new_record['host']}")
    return 0


def _route_chair_extend(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    session = a.label or "chair"
    pid, host = _leader_identity(a.pid)
    refuse_rc = _leader_refuse_dead_pid(a.pid) or _leader_refuse_hours(a.hours)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        if not chair.same_holder(record, session, pid, host):
            print(f"chair: not held by {session} (pid {pid}) on {host}")
            return 2
        new_record = chair.extend(record, datetime.datetime.now(datetime.UTC), a.hours)
        chair.write(runs_dir, new_record)
    print(f"chair extended until {new_record['until']}")
    return 0


def _route_chair_beat(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    session = a.label or "chair"
    pid, host = _leader_identity(a.pid)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        new_record, reason = chair.beat(record, session, pid, host, datetime.datetime.now(datetime.UTC), run_id=a.run, claude_session=chair.claude_session_from_env(os.environ))
        if new_record is None:
            print(reason)
            return 2
        lost = chair.renew_lease(runs_dir, session, pid, host)
        if lost:
            print(lost)
            return 2
        chair.write(runs_dir, new_record)
    print(f"chair heartbeat: {new_record['session']}")
    return 0


def _route_chair_release(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    session = a.label or "chair"
    pid, host = _leader_identity(a.pid)
    refuse_rc = _leader_refuse_dead_pid(a.pid)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        _new_record, reason = chair.release(record, session, pid, host)
        if reason:
            print(reason)
            return 2
        chair.release_lease(runs_dir, session, pid, host)
        chair.write(runs_dir, None)
    print(f"chair released: {record['session']}")
    return 0


def _route_chair_status(a: argparse.Namespace) -> int:
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    record, read_rc = _leader_read_or_refuse(runs_dir)
    if read_rc is not None:
        return read_rc
    now = datetime.datetime.now(datetime.UTC)
    alive = _leader_pid_alive(record)
    state = chair.liveness(record, alive, now, socket.gethostname(), _leader_heartbeat_minutes())
    if not a.json:
        _print_if_stale(record, state)
    if a.json:
        print(json.dumps({**(record or {}), "state": state}, indent=2))
    elif record is None:
        print("chair: none")
    else:
        left = chair.hours_left(record, now)
        window = "" if left is None else f" until {record['until']} ({'expired' if chair.expired(record, now) else f'{left:.1f}h left'})"
        print(f"chair: {record['session']} (pid {record['pid']}) on {record['host']} — {state}{window}")
    return 0


def _route_chair_clear(a: argparse.Namespace) -> int:
    """Holds `chair.locked` across the whole read-decide-unlink sequence, same as
    take/beat/release, so a `take` landing mid-clear cannot be discarded silently."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    with chair.locked(runs_dir):
        result = chair.clear(runs_dir, force=a.force)
    if result is None:
        print("chair: nothing cleared")
        return 0
    path, record = result
    print(f"chair cleared: {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')} [{path.name}]")
    return 0


def _route_chair_chat(a: argparse.Namespace) -> int:
    """`--read` prints the thread (or, with `--since`, only what's unread); otherwise
    appends one line, as the operator by default or, with `--as-leader`, only when this
    process's pid and host match the live lock's holder — a reply must never be shown
    from a session that is not the leader."""
    _profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    path = leader_chat.chat_path(runs_dir)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if a.read:
        thread = leader_chat.read_thread(existing, limit=50)
        entries = leader_chat.unread(thread, a.since)
        if a.json:
            print(json.dumps(entries))
        else:
            for entry in entries:
                print(f"{entry.get('at', '?')} {entry.get('from', '?')}: {entry.get('text', '')}")
        return 0
    if a.text is None:
        print("chat: TEXT required unless --read")
        return 2
    sender = "operator"
    if a.as_leader:
        record, read_rc = _leader_read_or_refuse(runs_dir)
        if read_rc is not None:
            return read_rc
        pid, host = _leader_identity()
        now = datetime.datetime.now(datetime.UTC)
        state = chair.liveness(record, _leader_pid_alive(record), now, host, _leader_heartbeat_minutes())
        if state != "live":
            print("chat: refusing --as-leader (no live lock held)")
            return 2
        if record.get("pid") != pid or record.get("host") != host:
            print(f"chat: refusing --as-leader (held by {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')})")
            return 2
        sender = record.get("session", "leader")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": datetime.datetime.now(datetime.UTC).isoformat(), "from": sender, "text": a.text}
    path.write_text(leader_chat.append_line(existing, entry), encoding="utf-8")
    print(f"chat: {sender}: {a.text}")
    return 0


_PROFILE_KEYS_FOR_LAUNCH = ("workspace_dir", "harness_dir", "team", "cartridges_dir", "provider_profile")


def _resolve_profile_or_refuse(a: argparse.Namespace):
    """Resolve the profile for `file`/`launch`, spec §7: unlike `context`,
    a missing profile or a profile missing a key `route.harness_argv` needs
    is a hard refusal here (exit 2), since both subcommands need real paths
    to write to or launch against, not a one-liner to print and carry on.

    Returns `(profile, None)` on success, `(None, 2)` after printing the
    reason.
    """
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return None, 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return None, 2
    for key in _PROFILE_KEYS_FOR_LAUNCH:
        if not profile.get(key):
            print(f"routing: {key} not set in profile {profile_path}")
            return None, 2
    return profile, None


def _item_shape(rel: str) -> tuple[str, tuple[str, ...]] | None:
    """(`kind`, `path_parts`) `queue_rows.parse_item` wants for one `_write_mapping` key, or `None` for a
    path with no queue row of its own — an `initiative.md`, or anything outside `work/`/`intake/` — the same
    split `_sync_filed_items` already draws between a synced item and the initiative doc beside it."""
    parts = Path(rel).parts
    if len(parts) == 4 and parts[0] == "work":
        return "task", parts[1:]
    if len(parts) in (2, 3) and parts[0] == "intake":
        return "intake", parts[1:]
    return None


def _write_mapping(mapping: dict, ws: Path, by: str = _UNLABELED):
    """Write `mapping` (relative path -> text) under `ws`; `None` on success, else the refusal to print.
    A `work/<initiative>/<phase>/<task>.md` or `intake/...md` path is filed store-first through
    `route.write_filed_item`, which prints its own warning or error; anything else (an `initiative.md`) is
    written directly, as it always was. Each `work/<initiative>/<phase>/<task>.md` written with a `state:`
    is then mirrored to the store the same as today, one call per file."""
    targets = {rel: ws / rel for rel in mapping}
    existing = [str(path) for path in targets.values() if path.exists()]
    if existing:
        return f"routing: refusing to overwrite existing path(s): {', '.join(existing)}"
    runs_dir = ws / "runs"
    for rel, path in targets.items():
        shape = _item_shape(rel)
        if shape is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mapping[rel], encoding="utf-8")
        elif route.write_filed_item(runs_dir, path, *shape, mapping[rel]) != 0:
            return f"routing: {path} was recorded in the store but its own file did not write; see above"
        print(str(path))
        _mirror_filed_state(rel, mapping[rel], runs_dir, by)
    return None


def _mirror_filed_state(rel: str, text: str, runs_dir: Path, by: str) -> None:
    parts = Path(rel).parts
    if len(parts) != 4 or parts[0] != "work":
        return
    fields = route.parse_frontmatter(text)[0]
    if not fields.get("state"):
        return
    line = store_cli.mirror_state(runs_dir, parts[1], str(fields.get("id") or Path(rel).stem), fields["state"], by)
    if line is not None:
        print(line)


def _sync_filed_items(mapping: dict, ws: Path, a: argparse.Namespace) -> None:
    """Run the per-item sync for each intake or work item `mapping` wrote, so its issue exists from birth.
    A failed sync (gh down, rate-limited) never fails the file: one line names the item and the way back."""
    rels = [rel for rel in mapping
            if (len(Path(rel).parts) == 2 and Path(rel).parts[0] == "intake")
            or (len(Path(rel).parts) == 4 and Path(rel).parts[0] == "work")]
    for rel in rels:
        item_id = route.parse_frontmatter(mapping[rel])[0].get("id") or Path(rel).stem
        sync = argparse.Namespace(item=item_id, workspace=str(ws), dry_run=False, project=None, profile=a.profile)
        if _sync_skipped(sync):
            print(f"route file: wrote {item_id}; not synced: tracker is none")
            continue
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = _route_sync(sync)
        except Exception as exc:
            rc, out = 1, io.StringIO(f"{type(exc).__name__}: {exc}")
        lines = out.getvalue().strip().splitlines()
        reason = f"; {lines[-1]}" if rc != 0 and lines else ""
        print(f"synced {item_id}" if rc == 0
              else f"route file: wrote {item_id}; issue sync failed (exit {rc}{reason}); run `route sync --item {item_id}`")


def _route_file_from_intake(a: argparse.Namespace, ws: Path, profile: dict) -> int:
    intake_path = Path(a.from_intake)
    if intake_path.parent.resolve() != (ws / "intake").resolve():
        print(f"routing: --from-intake must name a file directly under {ws / 'intake'}")
        return 2
    intake_text = _read_text_or_none(intake_path)
    if intake_text is None:
        print(f"routing: cannot read intake file {a.from_intake}")
        return 2
    fields, intake_body = route.parse_frontmatter(intake_text)
    [entry] = route.intake_entries({intake_path.name: intake_text})
    repo = fields.get("repo", "")
    candidates = route.surface_candidates(intake_body, repo)
    surfaces = [c for c in candidates if (Path(repo) / c).is_file()]
    print(f"surfaces: {', '.join(surfaces)}" if surfaces else "surfaces: none resolved from the intake body")
    budget_usd = profile.get("one_task_budget_usd", 2.0)
    try:
        mapping = route.initiative_files(
            entry["title"], intake_body, repo, phase=a.phase,
            surfaces=surfaces, budget_usd=budget_usd, slug=fields.get("slug"),
        )
    except ValueError as exc:
        print(f"routing: {exc}")
        return 2
    slug = route.slugify(fields.get("slug") or entry["title"])
    initiative_rel = f"work/{slug}/initiative.md"
    new_initiative_text, new_intake_text = route.link_intake(
        mapping[initiative_rel], intake_text, f"intake/{intake_path.name}", slug
    )
    mapping[initiative_rel] = new_initiative_text
    mapping[f"intake/done/{intake_path.name}"] = new_intake_text
    refusal = _write_mapping(mapping, ws, by=_holder_label(a))
    if refusal:
        print(refusal)
        return 2
    intake_path.unlink()
    print(f"removed: {intake_path}")
    _sync_filed_items(mapping, ws, a)
    return 0


def _route_file(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    if a.from_intake:
        return _route_file_from_intake(a, ws, profile)
    if not a.title or not a.repo:
        print("routing: --title and --repo are required unless --from-intake is given")
        return 2
    if a.body == "-":
        body = sys.stdin.read()
    elif a.body:
        body = _read_text_or_none(Path(a.body))
        if body is None:
            print(f"routing: cannot read body file {a.body}")
            return 2
    else:
        body = ""
    try:
        if a.intake:
            date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
            mapping = route.intake_file(a.title, body, a.repo, date)
        else:
            mapping = route.initiative_files(a.title, body, a.repo, phase=a.phase)
    except ValueError as exc:
        print(f"routing: {exc}")
        return 2
    refusal = _write_mapping(mapping, ws, by=_holder_label(a))
    if refusal:
        print(refusal)
        return 2
    _sync_filed_items(mapping, ws, a)
    return 0


def _run_argv(argv: list[str]) -> tuple[int, str, str]:
    """`(returncode, stdout, stderr)`; a missing executable is returncode 127, never an exception."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        return 127, "", str(exc)
    return r.returncode, r.stdout, r.stderr.strip()


def _fetch_listing(adapter, config) -> list | None:
    """The raw listing across `config.repos`; `None` after printing when a fetch fails. An adapter with no `list_argv` lists nothing; `pr_list_argv` adds its PRs."""
    builders = [b for b in (getattr(adapter, "list_argv", None), getattr(adapter, "pr_list_argv", None)) if b]
    # An adapter whose API wraps its listing (e.g. under `data`) supplies `unwrap(payload, repo)`.
    unwrap = getattr(adapter, "unwrap", None) or (lambda payload, _repo: payload)
    listing: list = []
    for repo in config.repos:
        for build in builders:
            rc, out, err = _run_argv(build(config, repo))
            try:
                listing.extend(unwrap(json.loads(out or "[]"), repo) if rc == 0 else ())
            except json.JSONDecodeError as exc:
                rc, err = 1, f"listing is not JSON: {exc}"
            except (KeyError, TypeError) as exc:
                rc, err = 1, f"listing has an unexpected shape: {type(exc).__name__}: {exc}"
            if rc != 0:
                print(f"routing: listing {repo} failed: {err}")
                return None
    return listing


def _pull_candidates(adapter, config, listing: list) -> tuple[dict, list[str]]:
    """Candidates by link, reading only entries `candidates` accepts, one entry at a time; an unparsable entry is a problem line."""
    found: dict = {}
    problems: list[str] = []
    for raw in listing:
        try:
            wanted = {ref.link for ref in adapter.candidates(config, [raw])}
            candidate = adapter.read(raw) if wanted else None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            name = next((str(raw[k]) for k in ("url", "link", "title") if k in raw), "an entry") if isinstance(raw, dict) else "an entry"
            problems.append(f"routing: skipping unreadable listing entry {name}: {type(exc).__name__}: {exc}")
            continue
        if candidate is not None and candidate.link in wanted:
            found[candidate.link] = candidate
    return found, problems


def _mark_argv(adapter, config, ref, rel: str) -> list[str]:
    """An adapter that sets `MARK_NEEDS_TOKEN_ENV` also gets the profile's `token_env`."""
    if getattr(adapter, "MARK_NEEDS_TOKEN_ENV", False):
        return adapter.mark_argv(ref, rel, config.token_env)
    return adapter.mark_argv(ref, rel)


def _pull_one(adapter, config, mapping: dict, found: dict, ws: Path, dry_run: bool) -> bool:
    """Write one planned intake file, then mark its source; True after printing why when either step failed."""
    (rel, text), = mapping.items()
    origin = found[route.parse_frontmatter(text)[0]["link"]]
    if dry_run:
        print(f"would write {rel}")
        return False
    if refusal := _write_mapping(mapping, ws):
        print(f"{refusal}; {origin.title!r} ({origin.link}) not filed")
        return True
    rc, _, err = _run_argv(_mark_argv(adapter, config, sources.Ref(origin.link, origin.repo), rel))
    if rc != 0:
        print(f"routing: wrote {rel}; marking {origin.link} failed (exit {rc}): {err}")
    return rc != 0


def _review_one(adapter, origin, profile: str, dry_run: bool) -> bool:
    """Review one PR under the pull's profile, then mark it so the next pull skips it; True after printing why when either step failed."""
    if dry_run:
        print(f"would review {origin.link}")
        return False
    rc, _, err = _run_argv(route.review_argv(origin.link, profile))
    if rc != 0:
        print(f"routing: review of {origin.link} failed (exit {rc}): {err}")
        return True
    rc, _, err = _run_argv(adapter.mark_argv(sources.Ref(origin.link, origin.repo, "pr"), ""))
    if rc != 0:
        print(f"routing: reviewed {origin.link}; marking it failed (exit {rc}): {err}")
    return rc != 0


def _route_pull(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    config = sources.source_config(profile, a.source)
    adapter = sources.adapter_for(a.source)
    if config is None or adapter is None:
        print(f"routing: no source {a.source!r} in the profile and adapters")
        return 2
    listing = _fetch_listing(adapter, config)
    if listing is None:
        return 2
    ws = Path(profile["workspace_dir"]).expanduser()
    intake_root = ws / "intake"
    known = sorted(intake_root.glob("*.md")) + sorted((intake_root / "done").glob("*.md"))
    links = sources.intake_links({str(p.relative_to(ws)): p.read_text(encoding="utf-8") for p in known})
    found, unreadable = _pull_candidates(adapter, config, listing)
    taken = frozenset(link for link in found if adapter.taken(link, links))
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    plan, reviews, refusals = route.pull_plan(
        tuple(found.values()), taken, profile.get("repo_map", {}), date=date, source=a.source
    )
    problems = [*unreadable, *refusals]
    for line in problems:
        print(line)
    if not plan and not reviews:
        print("routing: pull wrote nothing: no eligible candidates")
    failed = [_pull_one(adapter, config, mapping, found, ws, a.dry_run) for mapping in plan]
    failed += [_review_one(adapter, found[link], str(_profile_path(a)), a.dry_run) for link in reviews]
    return 2 if problems or any(failed) else 0


def _harness_ready_or_refuse(harness_dir: str):
    """spec §4/§7: `launch` refuses before anything starts when the
    harness venv is not where the profile says. Returns 2 after printing
    the reason, or `None` when the venv is ready.
    """
    shell_py = Path(harness_dir) / "shell.py"
    venv_python = Path(harness_dir) / ".venv" / "bin" / "python"
    if not shell_py.exists() or not venv_python.exists():
        print(f"routing: harness venv missing at {harness_dir}; install it (see the harness README)")
        return 2
    return None


def _route_sync_state_path(profile_path: Path) -> Path:
    """Where the resolved `owner/N` project persists across runs: a sidecar
    next to the profile, not a key inside it. `route.parse_profile` accepts
    a fixed field set (`route.py`'s `_KNOWN_KEYS`), and `project` is not one
    of them, so writing it into the profile itself would leave that file
    unparsable by every other route command from then on."""
    return profile_path.with_name(profile_path.name + ".route-sync-project")


def _resolve_sync_project(a: argparse.Namespace, items: list, state_path: Path):
    """`(project, None)` on success, `(None, 2)` after printing the reason.
    `project` comes back `None` (no error) only for a dry-run preview run
    before any project exists — nothing to create yet, nothing to preview
    against either."""
    if a.project:
        return a.project, None
    cached = _read_text_or_none(state_path)
    if cached and cached.strip():
        return cached.strip(), None
    owner = items[0].repo.split("/", 1)[0] if items and "/" in items[0].repo else ""
    if not owner:
        print("route sync: no --project given and no repo to derive a project owner from")
        return None, 2
    ok, found = route_sync_gh.find_project(subprocess.run, owner)
    if not ok:
        print(f"route sync: {found}")
        return None, 2
    if found is not None:
        return f"{owner}/{found}", None
    if a.dry_run:
        return None, None
    ok, number = route_sync_gh.create_project(subprocess.run, owner)
    if not ok:
        print(f"route sync: {number}")
        return None, 2
    project = f"{owner}/{number}"
    state_path.write_text(f"{project}\n")
    return project, None


def _sync_context(a: argparse.Namespace) -> tuple[str, str] | str:
    """`(workspace, tracker)` a sync runs against, or why the profile is unreadable.
    The policy file is read from `a.runs_dir` when a land passes one, else `<workspace>/runs`."""
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError as exc:
        return f"profile unreadable: {exc}"
    workspace = a.workspace or profile.get("workspace_dir") or "."
    runs_dir = Path(getattr(a, "runs_dir", None) or Path(workspace).expanduser() / "runs")
    return workspace, _resolved_tracker(profile, runs_dir)


def _sync_skipped(a: argparse.Namespace) -> bool:
    """True when the sync `a` describes would mirror nothing because the tracker is `none`."""
    context = _sync_context(a)
    return not isinstance(context, str) and context[1] == "none"


def _only_item(items: list[route_sync.Item], item_id: str | None) -> tuple[list[route_sync.Item], str | None]:
    """The items, narrowed to `item_id` when given, and a refusal line unless that leaves exactly one."""
    if not item_id:
        return items, None
    kept = [item for item in items if item.id == item_id]
    # Never fall through to the full listing: an unknown or shared id would run `item-list`.
    if len(kept) != 1:
        return kept, f"route sync: --item {item_id!r} matches {len(kept)} work-store items, expected exactly one"
    return kept, None


def _route_sync_registered(a: argparse.Namespace, name: str, mirror: object, workspace: str) -> int:
    """Drive a registered tracker through `sync`; no `gh` call is made on this path."""
    sync = getattr(mirror, "sync", None)
    if not callable(sync):
        print(f"route sync: tracker {name} has no sync(workspace, items, *, dry_run)")
        return 2
    items, refusal = _only_item(route_sync_gh.items_from_store(workspace), a.item)
    if refusal:
        print(refusal)
        return 2
    try:
        ok, lines = sync(workspace, items, dry_run=a.dry_run)
    except Exception as exc:
        print(f"route sync: tracker {name} failed: {type(exc).__name__}: {exc}")
        return 1
    for line in lines:
        print(line)
    return 0 if ok else 1


def _route_sync(a: argparse.Namespace) -> int:
    """Mirror the work store onto GitHub Projects: `--dry-run` renders the
    plan and touches nothing that outlives the run; otherwise `execute`
    runs it and stops at the first failed `gh` call. Refuses at once, exit
    2, when `gh` is not authenticated. The tracker is checked first: `none`
    exits 0, an unknown tracker exits 2, and a registered tracker is driven
    through its own `sync`, never through `gh`."""
    context = _sync_context(a)
    if isinstance(context, str):
        print(f"route sync: {context}")
        return 2
    workspace, tracker_choice = context
    profile_path = _profile_path(a)
    if tracker_choice == "none":
        print("route sync: tracker is none; set tracker: github-projects in the profile to mirror the work store")
        return 0
    mirror = tracker.tracker_for(tracker_choice)
    if mirror is None:
        print(f"route sync: no tracker named {tracker_choice} (built in: none, github-projects)")
        return 2
    if mirror is not route_sync_gh:
        return _route_sync_registered(a, tracker_choice, mirror, workspace)
    if not route_sync_gh.auth_ok(subprocess.run):
        print("route sync: gh is not authenticated; run `gh auth login`")
        return 2
    items, refusal = _only_item(route_sync_gh.items_from_store(workspace), a.item)
    if refusal:
        print(refusal)
        return 2
    project, rc = _resolve_sync_project(a, items, _route_sync_state_path(profile_path))
    if rc is not None:
        return rc
    repo_names = sorted({item.repo for item in items if item.repo})
    if a.item:
        ok, result = route_sync_gh.existing_item(subprocess.run, items[0].repo, items[0].issue, project)
    else:
        ok, result = route_sync_gh.existing(subprocess.run, repo_names, project)
    if not ok:
        print(f"route sync: {result}")
        return 2
    issues, project_items, item_node_ids = result
    steps = route_sync.plan(items, issues, project_items, "github-projects")
    if a.dry_run:
        print("\n".join(route_sync.render(steps)))
        return 0
    log = route_sync_gh.execute(steps, subprocess.run, project, workspace, items, item_node_ids)
    for kind, _, detail in log:
        print(f"{kind}: {detail}")
    hard_failure = any(not step_ok for kind, step_ok, _ in log if kind != "refuse")
    return 1 if hard_failure else 0


def _route_launch(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    host, host_rc = _lane_host_or_refuse(a, profile)
    if host_rc is not None:
        return host_rc
    harness_dir = profile.get("harness_dir", "")
    venv_rc = _harness_ready_or_refuse(harness_dir)
    if venv_rc is not None:
        return venv_rc
    runs_dir = Path(profile["workspace_dir"]).expanduser() / "runs"
    if a.graph == "epic":
        _merge_initiative_tickets(Path(a.initiative).expanduser())
        # Only --include-blocked lifts this guard; --force never does.
        initiative_id = Path(a.initiative).expanduser().name
        held = route.launch_blockers([
            item for item in _work_items(runs_dir.parent) if item["initiative"] == initiative_id
        ])
        if held and not a.include_blocked:
            print(f"routing: {initiative_id} has a ready task behind blocked {', '.join(held)}; pass --include-blocked to launch anyway")
            return 2
        if held:
            print(f"override: launching {initiative_id} despite blocked {', '.join(held)} (--include-blocked)")
    guard_rc = _leader_guard_or_refuse(runs_dir, _holder_label(a), a.force, claim=not a.no_claim)
    if guard_rc is not None:
        return guard_rc
    usage_code, usage_lines = route.launch_gate(
        _usage_assessment(
            runs_dir, profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
        ), a.force
    )
    for line in usage_lines:
        print(line)
    if usage_code is not None:
        return usage_code
    overlaid_provider_profile = None
    if a.tier_ceiling is not None or a.effort_ceiling is not None:
        provider_path = Path(profile["provider_profile"]).expanduser()
        provider_text = _read_text_or_none(provider_path)
        try:
            parsed = yaml.safe_load(provider_text) if provider_text is not None else None
        except yaml.YAMLError as exc:
            print(f"routing: provider profile at {provider_path} is not valid YAML: {exc}")
            return 2
        if not isinstance(parsed, dict):
            print(f"routing: no provider profile to apply a ceiling to at {provider_path}")
            return 2
        overlaid_provider_profile = route.overlay(parsed, a.tier_ceiling, a.effort_ceiling)
        if isinstance(overlaid_provider_profile, str):
            print(overlaid_provider_profile)
            return 2
    runs_dir.mkdir(parents=True, exist_ok=True)

    if a.graph in ("epic", "rescue"):
        initiative_dir = Path(a.initiative).expanduser()
        initiative_md = initiative_dir / "initiative.md"
        text = _read_text_or_none(initiative_md)
        if text is None:
            print(f"routing: no initiative.md at {initiative_md}")
            return 2
        fields, _ = route.parse_frontmatter(text)
        repo = a.repo or fields.get("repo")
        if not repo:
            print(f"routing: no --repo given and no repo: in {initiative_md}")
            return 2
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"routing: cannot read git status for {repo}: {exc}")
            return 2
        if status.stdout.strip():
            print(f"routing: {repo} has uncommitted changes:\n{status.stdout}")
            return 2
        prefix = initiative_dir.name
        already = _refuse_if_already_running(runs_dir, prefix)
        if already is not None:
            print(already)
            return 2
        if getattr(a, "run_id", None) is None:
            run_id = route.next_run_id(_taken_run_names(runs_dir), prefix)
        else:
            taken = remote_lane.refuse_taken_run_id(a.run_id, set(_taken_run_names(runs_dir)))
            if taken is not None:
                print(taken)
                return 2
            run_id = a.run_id
        needs = {"initiative": str(initiative_dir), "repo": repo}
        if a.graph == "rescue":
            needs["task"] = a.task
        elif a.fix_attempts is not None:
            needs["fix_attempts"] = a.fix_attempts
        env_repo = repo
    elif a.graph == "decompose":
        idea_path = Path(a.idea)
        if not idea_path.exists():
            print(f"routing: no idea file at {idea_path}")
            return 2
        workspace_dir = Path(profile["workspace_dir"]).expanduser()
        initiative_dir = workspace_dir / "work" / a.initiative_id
        initiative_md = initiative_dir / "initiative.md"
        idea_fields, idea_body = route.parse_frontmatter(idea_path.read_text(encoding="utf-8"))
        intake = os.path.relpath(idea_path, workspace_dir)
        if initiative_md.exists() and any(initiative_dir.glob("*/*.md")):
            print("routing: initiative.md kept: tickets exist")
        elif initiative_md.exists():
            fields, body = route.parse_frontmatter(initiative_md.read_text(encoding="utf-8"))
            fresh = idea_path.stat().st_mtime <= initiative_md.stat().st_mtime and idea_body == body
            if fresh:
                print(f"routing: {initiative_md} already exists, left untouched")
            elif a.dry_run:
                print(f"dry-run: would write {initiative_md}")
            else:
                content = route.initiative_text(
                    fields.get("id", a.initiative_id), fields.get("title", ""),
                    fields.get("repo", ""), fields.get("intake", intake), idea_body,
                )
                initiative_md.write_text(content, encoding="utf-8")
                print(f"routing: initiative.md refreshed from {idea_path}")
        else:
            initiative_content = route.initiative_text(
                a.initiative_id, idea_fields.get("title", ""), idea_fields.get("repo", ""), intake, idea_body,
            )
            if a.dry_run:
                print(f"dry-run: would write {initiative_md}")
            else:
                initiative_md.parent.mkdir(parents=True, exist_ok=True)
                initiative_md.write_text(initiative_content, encoding="utf-8")
        run_id = route.next_run_id(_taken_run_names(runs_dir), a.initiative_id)
        needs = {"idea": a.idea, "initiative_id": a.initiative_id}
        env_repo = ""
    else:  # cos
        already = _refuse_if_already_running(runs_dir, "cos")
        if already is not None:
            print(already)
            return 2
        run_id = route.next_run_id(_taken_run_names(runs_dir), "cos")
        needs = {}
        env_repo = ""

    launch_profile = profile
    overlaid_path = None
    if overlaid_provider_profile is not None:
        overlaid_path = runs_dir / f"{run_id}.provider-profile.yaml"
        launch_profile = {**profile, "provider_profile": str(overlaid_path)}
    argv = route.harness_argv(launch_profile, a.graph, run_id, **needs)
    log_path = runs_dir / f"{run_id}.log"
    pid_path = runs_dir / f"{run_id}.pid"
    trace_dir = runs_dir / f"{run_id}-trace"
    env = route.child_env(dict(os.environ), harness_dir=harness_dir, repo=env_repo, trace_dir=str(trace_dir))

    if host is not None and a.dry_run:
        for remote_argv in remote_launch.launch_plan(host, Path(a.initiative).name, run_id, _holder_label(a)):
            print(f"dry-run: {shlex.join(remote_argv)}")
        print(f"host {host.name}")
        return 0

    if a.dry_run:
        print(f"dry-run: {' '.join(argv)}")
        print(f"pid {pid_path}")
        print(f"log {log_path}")
        print(f"trace {trace_dir}")
        return 0

    if host is not None:
        return _route_launch_on_host(a, host, runs_dir, run_id, env_repo or None)

    if overlaid_path is not None:
        overlaid_path.write_text(yaml.safe_dump(overlaid_provider_profile, sort_keys=True), encoding="utf-8")
        (runs_dir / f"{run_id}.ceiling.json").write_text(
            json.dumps({
                "requested": {"tier": a.tier_ceiling, "effort": a.effort_ceiling},
                "applied": {"tier": a.tier_ceiling, "effort": a.effort_ceiling},
                "profile": str(overlaid_path),
            }, indent=2),
            encoding="utf-8",
        )

    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    pid_path.write_text(str(proc.pid))
    now = datetime.datetime.now(datetime.UTC)
    lock_record, lock_refuse_rc = _leader_read_or_refuse(runs_dir)
    launched_by = None if lock_refuse_rc is not None else _leader_launched_by(lock_record, _leader_pid_alive(lock_record), now, socket.gethostname(), _leader_heartbeat_minutes())
    launched_payload = {"launched_by": launched_by} if launched_by is not None else {}
    (runs_dir / f"{run_id}.launched.json").write_text(
        json.dumps({**launched_payload, "at": now.isoformat()}), encoding="utf-8",
    )
    print(f"run {run_id}")
    print(f"pid {pid_path}")
    print(f"log {log_path}")
    return 0


def _remote_edge(cwd: Path) -> tuple[Callable[[list[str]], int], Callable[[str], str] | None]:
    """Edge: the one seam that runs rsync and ssh for `--on`; `cwd` is the workspace the `work/<id>` copy is relative to."""

    def run(argv: list[str]) -> int:
        try:
            return subprocess.run(argv, cwd=cwd).returncode
        except OSError as exc:
            print(f"{argv[0]}: {exc}")
            return 127

    return run, None


def _lane_host_candidates(profile_text: str, rows: Sequence[Mapping]) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """The table's active hosts when the table has rows, else the profile's `lane_hosts`."""
    parsed = _profile_lane_hosts(profile_text)
    if not rows:
        return parsed
    return host_cmd.host_rows_to_lane_hosts(rows, () if isinstance(parsed, lane_hosts.LaneHostError) else parsed)


def _shadowed_note(profile_text: str, table_names: Sequence[str]) -> str | None:
    """One line naming the profile lane hosts the table overrides, or None when the table is empty or overrides none."""
    parsed = _profile_lane_hosts(profile_text)
    names = [] if not table_names or isinstance(parsed, lane_hosts.LaneHostError) else host_cmd.shadowed(parsed, table_names)
    return f"profile lane_hosts not in the hosts table are not lane hosts while it has rows: {', '.join(names)}; add them with `cox host add`" if names else None


def _lane_host_or_refuse(a: argparse.Namespace, profile: Mapping) -> tuple[lane_hosts.LaneHost | None, int | None]:
    """Exit code 2 after printing the refusal; no host when `--on` is absent."""
    name = getattr(a, "on", None)
    if name is None:
        return None, None
    # The remote command carries only the initiative, run id and label; a ceiling dropped silently would run uncapped.
    dropped = [flag for flag, value in (
        ("--tier-ceiling", getattr(a, "tier_ceiling", None)),
        ("--effort-ceiling", getattr(a, "effort_ceiling", None)),
        ("--fix-attempts", getattr(a, "fix_attempts", None)),
    ) if value is not None]
    if dropped:
        print(f"routing: --on does not carry {', '.join(dropped)} to the host; launch without them")
        return None, 2
    rows = run_store.hosts(Path(profile.get("workspace_dir", "")).expanduser() / "runs")
    profile_text = _read_text_or_none(_profile_path(a)) or ""
    hosts = _lane_host_candidates(profile_text, rows)
    if isinstance(hosts, lane_hosts.LaneHostError):
        print(f"routing: {hosts.message}")
        return None, 2
    host = lane_hosts.find_lane_host(hosts, name)
    if host is None:
        print(f"routing: unknown lane host: {name}")
        print(f"configured: {', '.join(h.name for h in hosts) or 'none'}")
        note = _shadowed_note(profile_text, [str(r["name"]) for r in rows])
        if note:
            print(note)
        return None, 2
    if not host.workspace_dir:
        print(f"routing: host {name} has no workspace_dir: run `cox host beat {name}` on it, or list it under lane_hosts in the profile")
        return None, 2
    return host, None


def _host_auth_output(argv: list[str]) -> str:
    """Edge: stdout and stderr of `claude auth status` on the host; an ssh that cannot run gives its error text."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        return f"{argv[0]}: {exc}"
    return done.stdout + done.stderr


def _route_launch_on_host(
    a: argparse.Namespace, host: lane_hosts.LaneHost, runs_dir: Path, run_id: str, repo: str | None = None,
) -> int:
    """Copies the initiative to `host` and starts the lane there; writes only `<run>.remote.json`, and only on success."""
    run, locate = _remote_edge(runs_dir.parent)
    launched_at = datetime.datetime.now(datetime.UTC).isoformat()
    preflight = remote_doctor.auth_verdict(_host_auth_output(ssh_argv(host.ssh, auth_status_argv())))
    result = remote_launch.launch_on_host(
        host, Path(a.initiative).name, run_id, _holder_label(a), launched_at, run, locate, repo, preflight=preflight
    )
    if isinstance(result, remote_launch.LaunchError):
        print(f"routing: launch on {host.name} failed at {result.step}: {result.message}")
        return 2
    remote_lane.remote_record_path(runs_dir, run_id).write_text(json.dumps(result), encoding="utf-8")
    print(f"run {run_id}")
    print(f"host {host.name}")
    return 0


def _remote_fetch_facts(runs_dir: Path, run: str) -> tuple[bool, str | None]:
    """Edge. (lease_released, ended_at) for `run` from the store, as the docket reads them: a live lease, or no ended span, keeps a lane unfetched."""
    live = {lane.run for lane in run_store.live_lanes(runs_dir, _now_iso())}
    ended = {span_run: ended_at for span_run, _, ended_at in run_store.run_spans(runs_dir, "")}
    return run not in live, ended.get(run)


def _remote_capture(cwd: Path) -> Callable[[list[str]], str | None]:
    """Edge: runs an argv and returns its stdout, None when it cannot run or exits non-zero."""

    def capture(argv: list[str]) -> str | None:
        try:
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        except OSError:
            return None
        return done.stdout if done.returncode == 0 else None

    return capture


def _task_pairs_from_listing(text: str) -> list[tuple[str, str]]:
    """Pure: (phase, task) for each `<phase>/<task>.json` file in an `rsync -r --list-only` listing of a run's `tasks/`."""
    fields = (line.split(None, 4) for line in text.splitlines())
    paths = (f[4].split("/") for f in fields if len(f) == 5 and f[0].startswith("-"))
    return [(path[0], path[1].removesuffix(".json")) for path in paths if len(path) == 2 and path[1].endswith(".json")]


def _store_holds_every_record(pairs: list[tuple[str, str]], read: Callable[[str, str], dict | None]) -> bool:
    """Pure: True when `read` finds a record for every (phase, task) pair; no pairs is False."""
    held = {task for phase, task in pairs if read(phase, task) is not None}
    return remote_lane.records_already_in_store([task for _, task in pairs], held)


def _without_task_records(argv: list[str], run_dest: str) -> list[str]:
    """Pure: the run-directory rsync (the one landing at `run_dest`) with `tasks/` excluded; any other argv is unchanged."""
    if argv[:1] == ["rsync"] and argv[-1] == run_dest:
        return [*argv[:2], "--exclude=/tasks/", *argv[2:]]
    return argv


def _store_task_records(runs_dir: Path, run_id: str, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict | None]:
    """Edge: each pair's task record from the store; a pair the store cannot read maps to None."""

    def read(phase: str, task: str) -> dict | None:
        try:
            return run_store.task_record(runs_dir, run_id, phase, task)
        except (OSError, ValueError):
            return None

    return {(phase, task): read(phase, task) for phase, task in pairs}


def _branch_only_repos(
    runs_dir: Path, host: lane_hosts.LaneHost, locate: Callable[[str], str] | None, run_id: str, recorded: list[str],
) -> list[str] | None:
    """Edge. The repos to fetch branches for when the store holds every task record of the remote run, else None.

    The records are not copied, so the repos come from the store records, then from the remote record. With neither the
    branch fetch has nowhere to go, and the caller copies the records as before."""
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    tasks_dir = f"{host.workspace_dir.rstrip('/')}/runs/{run_id}/tasks/"
    listing = _remote_capture(runs_dir.parent)(["rsync", "-r", "--list-only", place(tasks_dir)])
    pairs = _task_pairs_from_listing(listing or "")
    records = _store_task_records(runs_dir, run_id, pairs)
    complete = _store_holds_every_record(pairs, lambda phase, task: records[(phase, task)])
    if remote_lane.fetch_scope(complete) != "branches":
        return None
    stored = [r["repo"] for r in records.values() if r is not None and isinstance(r.get("repo"), str) and r["repo"]]
    return list(dict.fromkeys(stored or recorded)) or None


def _landed_elsewhere(
    runs_dir: Path, host: lane_hosts.LaneHost, locate: Callable[[str], str] | None, run_id: str, ended: bool,
) -> bool:
    """Edge. `remote_lane.landed_elsewhere` for the run: the store is read only once it has ended, and the remote is
    listed only once every stored task is done. A task whose initiative cannot be found counts as not done, and a
    listing that fails is passed on as None, so the caller fetches as before."""
    work_root = runs_dir.parent / "work"

    def state(task: str) -> str | None:
        initiative = _initiative_of(work_root, task)
        return None if initiative is None else run_store.task_state_of(runs_dir, initiative, task)

    states = {task: state(task) for task in run_store.run_task_ids(runs_dir, run_id)} if ended else {}
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    tasks_dir = f"{host.workspace_dir.rstrip('/')}/runs/{run_id}/tasks/"
    listing = _remote_capture(runs_dir.parent)(["rsync", "-r", "--list-only", place(tasks_dir)]) if remote_lane.all_landed(states) else None
    listed = None if listing is None else [task for _, task in _task_pairs_from_listing(listing)]
    return remote_lane.landed_elsewhere(states, listed, ended)


def _fetch_one(runs_dir: Path, hosts, run_id: str, mode: str = "files") -> tuple[str, list[str], str]:
    """Edge. (outcome, lines, host name) for one remote run; outcome is fetched, live or failed.

    Under `mode` "store" the task records are not copied when the store already holds every one of them.
    An ended run whose stored tasks are all done, and whose remote lists no task the store lacks, is marked fetched
    with nothing pulled. The remote is listed to check that, so an unreachable remote is fetched as before and fails there."""
    text = _read_text_or_none(remote_lane.remote_record_path(runs_dir, run_id))
    record = remote_lane.parse_remote_record(text) if text is not None else None
    if record is None:
        return "failed", [f"fetch: no {run_id}.remote.json in {runs_dir}; the run was not launched with --on"], ""
    if isinstance(hosts, lane_hosts.LaneHostError):
        return "failed", [f"fetch: {hosts.message}"], ""
    host = lane_hosts.find_lane_host(hosts, record["host"])
    if host is None:
        return "failed", [f"fetch: host {record['host']} is no longer in the profile lane_hosts",
                          f"configured: {', '.join(h.name for h in hosts) or 'none'}"], ""
    lease_released, ended_at = _remote_fetch_facts(runs_dir, run_id)
    run, locate = _remote_edge(runs_dir.parent)
    ended = remote_fetch.refuse_unended(lease_released, ended_at) is None
    if mode == "store" and _landed_elsewhere(runs_dir, host, locate, run_id, ended):
        remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(remote_lane.landed_elsewhere_marker(_now_iso())))
        return "fetched", [remote_lane.landed_elsewhere_line(run_id)], host.name
    recorded = [record["repo"]] if record.get("repo") else []
    only = _branch_only_repos(runs_dir, host, locate, run_id, recorded) if mode == "store" else None
    dest = f"{str(runs_dir).rstrip('/')}/{run_id}/"

    def run_cmd(argv: list[str]) -> int:
        return run(argv if only is None else _without_task_records(argv, dest))

    def repo_paths(run_dir: Path) -> list[str]:
        return (remote_fetch.task_repos(run_dir) or recorded) if only is None else only

    result = remote_fetch.fetch_run(host, run_id, runs_dir, repo_paths, run_cmd, locate,
                                    lease_released=lease_released, ended_at=ended_at)
    if isinstance(result, remote_fetch.FetchError):
        detail = f"{run_id} is still live on {host.name}: {result.message}" if result.step == "refuse" else result.message
        return ("live" if result.step == "refuse" else "failed"), [f"fetch: {result.step}: {detail}"], host.name
    marker = {"fetched_at": _now_iso(), "repos": list(result)}
    remote_lane.fetched_record_path(runs_dir, run_id).write_text(json.dumps(marker))
    if only is not None:
        print(f"{run_id}: task records skipped, the store holds them")
    return "fetched", [f"run {run_id}", f"host {host.name}", "\n".join(f"repo {repo}" for repo in result)], host.name


def _runs_fetch_all(runs_dir: Path, hosts, mode: str = "files") -> int:
    """Fetches every remote run with no `.fetched.json` marker; a live run is skipped, a failed fetch makes the exit 2."""
    suffix = ".remote.json"
    remote_runs = [p.name[: -len(suffix)] for p in sorted(runs_dir.glob(f"*{suffix}"))]
    fetched = {run for run in remote_runs if remote_lane.fetched_record_path(runs_dir, run).exists()}
    todo = remote_lane.unfetched(remote_runs, fetched)
    if not todo:
        print("nothing to fetch")
        return 0
    failed = False
    for run_id in todo:
        outcome, lines, host = _fetch_one(runs_dir, hosts, run_id, mode)
        if outcome == "live":
            print(f"{run_id}: still live on {host}")
        elif outcome == "fetched":
            marker = _read_text_or_none(remote_lane.fetched_record_path(runs_dir, run_id))
            print(remote_lane.landed_elsewhere_line(run_id) if remote_lane.is_landed_elsewhere_marker(marker) else f"{run_id}: fetched from {host}")
        else:
            failed = True
            print("\n".join([f"{run_id}: {lines[0]}", *lines[1:]]))
    return 2 if failed else 0


def _runs_fetch(a: argparse.Namespace) -> int:
    """Pulls an ended remote run's records, log and branches to this machine; writes nothing in the store."""
    if bool(a.run_id) == bool(a.all):
        print("fetch: give one run id, or --all")
        return 2
    runs_dir, reason = _runs_dir_for_land(a)
    if runs_dir is None:
        print(f"fetch: {reason}")
        return 2
    hosts = _profile_lane_hosts(_read_text_or_none(_profile_path(a)) or "")
    mode = work_state.work_state_mode(_lake_provider(a)[0])
    if a.all:
        return _runs_fetch_all(runs_dir, hosts, mode)
    outcome, lines, _ = _fetch_one(runs_dir, hosts, a.run_id, mode)
    print("\n".join(lines))
    return 0 if outcome == "fetched" else 2


def _merge_initiative_tickets(initiative_dir: Path) -> list[dict]:
    """`route.merge_same_phase` on the tickets under `initiative_dir`: the
    same glob-and-`parse_frontmatter` read `_route_lint` used to do alone,
    now also the write-back. The merged ticket and every `dropped` member
    are rewritten in place with `route.merged_ticket_text`, which keeps
    every header line the merge did not change. One `merged: ...` line
    prints per merge, and the returned list drops the `dropped` entries:
    they are already folded into a survivor, so a caller linting or
    launching next must not see them beside it.
    """
    paths = sorted(initiative_dir.glob("*/*.md"))
    texts = [path.read_text(encoding="utf-8") for path in paths]
    items = [
        {
            "task": fields.get("id", path.stem),
            "id": fields.get("id", path.stem),
            "title": fields.get("title", fields.get("id", path.stem)),
            "state": fields.get("state", "todo"),
            "phase": fields.get("phase", path.parent.name),
            "surfaces": fields.get("surfaces", []),
            "body": body,
            "needs": fields.get("needs", []),
        }
        for path, text in zip(paths, texts)
        for fields, body in [route.parse_frontmatter(text)]
    ]
    merged_items, merges = route.merge_same_phase(items)
    touched = {member for merge in merges for member in merge["members"]}
    for path, text, item in zip(paths, texts, merged_items):
        if item["id"] in touched:
            path.write_text(route.merged_ticket_text(text, item), encoding="utf-8")
    for line in route.merge_report_lines(merges):
        print(line)
    return [item for item in merged_items if item.get("state") != "dropped"]


def _route_lint(a: argparse.Namespace) -> int:
    """work-shape.md §3: `cox route lint <initiative>`, before dispatch.
    `--repo` falls back to the initiative's own `repo:` frontmatter; only
    when neither names one does the reach rule stand down, and that is
    said once here rather than left for `lint_items` to flag every path.
    """
    initiative_dir = Path(a.initiative_dir)
    items = _merge_initiative_tickets(initiative_dir)
    repo = a.repo
    if not repo:
        init_path = initiative_dir / "initiative.md"
        if init_path.exists():
            init_fields, _ = route.parse_frontmatter(init_path.read_text(encoding="utf-8"))
            repo = init_fields.get("repo")
    if not repo:
        print(f"routing: no repo found for {a.initiative_dir}; reach check skipped")
    problems = route.lint_items(items, repo, ("pytest", "git status", "git diff"))
    for problem in problems:
        print(f"{problem.task} {problem.rule}: {problem.detail} -> {problem.fix}")
    return 2 if any(p.rule in ("reach", "coupling") for p in problems) else 0


def _route_groups(a: argparse.Namespace) -> int:
    """work-shape.md §5: `cox route groups` prints the newest
    `plans/intake-groups/<date>.md` file under the profile's workspace, or
    `no groups filed` when none exist.
    """
    profile_path = _profile_path(a)
    text = _read_text_or_none(profile_path)
    if text is None:
        print(f"routing: no profile at {profile_path}")
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    groups_dir = Path(workspace).expanduser() / "plans" / "intake-groups"
    names = sorted(p.name for p in groups_dir.glob("*.md")) if groups_dir.is_dir() else []
    latest = route.latest_groups_file(names)
    if latest is None:
        print("no groups filed")
        return 0
    print(str(groups_dir / latest))
    return 0


def _route_launch_sweep(a: argparse.Namespace) -> int:
    """work-shape.md §1: no harness graph exists yet, so only `--dry-run` runs."""
    argv = route.build_sweep_argv(a.idea, a.initiative_id, a.label)
    if a.dry_run:
        print(f"dry-run: {' '.join(argv)}")
        return 0
    print("routing: sweep has no harness graph to launch yet; use --dry-run")
    return 2


_CORE_PROBE_SCRIPT = '''
import json, os, sys

cartridges_dir, team, *roots = sys.argv[1:]
out = {"import": None, "load": None, "indexed": {}, "resolved": None, "skill_index": {}, "layers": None}
try:
    from importlib.metadata import entry_points
    out["plugins"] = {g: sorted(ep.name for ep in entry_points(group=g))
                      for g in ("coxswain.system_one", "coxswain.runners")}
except Exception:
    pass  # left unset: the doctor row reads "not checked", never an empty claim
try:
    from core.cartridge import load
    from core.skills import index_from_roots
except Exception as exc:
    out["import"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(out)); raise SystemExit(0)
try:
    index = index_from_roots(roots)
    out["indexed"] = {root: len(index_from_roots([root])) for root in roots}
    out["skill_index"] = {name: [str(p) for p in paths] for name, paths in index.items()}
except Exception as exc:
    # An empty mapping would read downstream as "no roots configured"; a zero
    # per root fails the skills row honestly, and the cartridge row names why.
    out["indexed"] = {root: 0 for root in roots}
    out["load"] = f"skill index failed: {type(exc).__name__}: {exc}"
    print(json.dumps(out)); raise SystemExit(0)
try:
    out["resolved"] = load(team, cartridges_dir, skill_index=index)
except Exception as exc:
    out["load"] = f"{type(exc).__name__}: {exc}"
try:
    from core.cartridge import layers
    out["layers"] = [[label, resolved] for label, resolved in layers(team, cartridges_dir, skill_index=index)]
except Exception as exc:
    # Never an empty list: a chain one layer short must be visibly unknown,
    # not silently read as "nothing set anything".
    out["layers"] = None
    out["layers_error"] = f"{type(exc).__name__}: {exc}"
overlay_text = os.environ.get("AGENT_TOOLS_OVERLAY_TEXT")
if overlay_text is None:
    out["overlay_errors"] = None
else:
    try:
        from core.cartridge import overlay_errors
        out["overlay_errors"] = overlay_errors(overlay_text)
    except Exception as exc:
        out["overlay_errors"] = [f"{type(exc).__name__}: {exc}"]
print(json.dumps(out))
'''


def _run_core_probe(python_path: str, cartridges_dir: str, team: str, skills_roots: list, raw_roots: list | None = None,
                     overlay_text: str | None = None) -> dict:
    """One `python -c` call into the harness venv (spec: setup doctor). Any
    failure to get parseable JSON back is folded into `core_import` as the
    stderr tail, never raised."""
    env = dict(os.environ)
    if overlay_text is not None:
        env["AGENT_TOOLS_OVERLAY_TEXT"] = overlay_text
    try:
        proc = subprocess.run(
            [python_path, "-c", _CORE_PROBE_SCRIPT, cartridges_dir, team, *skills_roots],
            capture_output=True, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"core_import": str(exc)}
    try:
        parsed = json.loads(proc.stdout)
    except ValueError:
        tail = (proc.stderr or "").strip().splitlines()
        return {"core_import": tail[-1] if tail else f"core probe exited {proc.returncode} with no JSON"}
    indexed = parsed.get("indexed", {}) or {}
    if raw_roots and len(raw_roots) == len(skills_roots):
        # The probe saw expanded paths; the core keys its skills row by the
        # profile's own strings, so map the counts back by position.
        indexed = {raw: indexed.get(exp, 0) for raw, exp in zip(raw_roots, skills_roots)}
    facts = {"core_import": parsed.get("import"), "cartridge_load": parsed.get("load"),
             "skill_roots_indexed": indexed, "resolved": parsed.get("resolved"),
             "skill_index": parsed.get("skill_index", {})}
    if "overlay_errors" in parsed:
        facts["overlay_errors"] = parsed["overlay_errors"]
    if "plugins" in parsed:
        facts["plugins_harness"] = parsed["plugins"]
    # Exactly one of `provenance`/`provenance_error` is set below, on every
    # path: a fact dict carrying neither would read as "nothing to report"
    # rather than "the walk never happened", which is the silent-mislabel
    # failure this probe exists to rule out.
    parsed_layers = parsed.get("layers")
    if parsed_layers is None:
        facts["provenance_error"] = parsed.get("layers_error") or "the probe exited before the layer walk"
    elif not parsed_layers:
        facts["provenance_error"] = "the loader returned no layers"
    else:
        try:
            facts["provenance"] = provenance.attribute([(label, resolved) for label, resolved in parsed_layers])
        except Exception as exc:
            facts["provenance_error"] = f"{type(exc).__name__}: {exc}"
    return facts


def _provider_command(text) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("command:"):
            # A flat YAML scalar may carry a trailing comment (`command: claude  # ...`);
            # the command is the first token, never the comment.
            value = stripped.partition(":")[2].split("#", 1)[0].strip().strip("\"'")
            return value.split()[0] if value else None
    return None


def _provider_facts(provider_profile_path: str) -> dict:
    command = _provider_command(_read_text_or_none(Path(provider_profile_path)))
    if command is None:
        return {"provider_command": None, "provider_on_path": False}
    facts = {"provider_command": command, "provider_on_path": shutil.which(command) is not None}
    if facts["provider_on_path"]:
        try:
            proc = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=20)
            lines = (proc.stdout or proc.stderr or "").strip().splitlines()
            facts["provider_version"] = lines[0] if lines else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    return facts


_NO_STORE_YET = "no store yet (it is created by the first run)"
_NO_RUNS_TABLE = "the store answers but has no runs table yet (it is created by the first run)"
_STORE_CONNECT_TIMEOUT_S = 5


def _sqlite_store_file(url: str) -> Path:
    """The file `connect_readonly_url` opens for a non-Postgres URL."""
    return Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url)


def _with_connect_timeout(url: str) -> str:
    """The Postgres URL with a connect timeout added unless it sets one."""
    parts = urllib.parse.urlsplit(url)
    if "connect_timeout" in urllib.parse.parse_qs(parts.query):
        return url
    query = "&".join(q for q in (parts.query, f"connect_timeout={_STORE_CONNECT_TIMEOUT_S}") if q)
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _store_error(exc: Exception) -> str:
    """A fixed message for a known cause, else the exception class; driver text can quote the URL, so it is never passed on."""
    if str(exc) == store_dialect.MISSING_PSYCOPG:
        return store_dialect.MISSING_PSYCOPG
    if type(exc).__name__ == "UndefinedTable" or (isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc)):
        return _NO_RUNS_TABLE
    return f"the store did not answer ({type(exc).__name__})"


def _store_facts(provider_profile: str, workspace_dir: str) -> dict:
    """Edge. Whether the run store answers a read; the URL never leaves this function."""
    url = store_url.resolve_store_url(provider_profile, Path(workspace_dir) / "runs")
    kind = store_url.describe_store(url).removeprefix("store: ")
    try:
        postgres = store_dialect.is_postgres(url)
    except ValueError:  # urlsplit rejects e.g. an unclosed `[`; the message would quote the URL
        return {"kind": kind, "reachable": False, "runs": None, "fresh": False, "error": "storage_url is not a valid URL"}
    # `other` is a bare path with no scheme, which `connect_readonly_url` opens as a SQLite file.
    if not postgres and kind not in ("sqlite", "other"):
        return {"kind": kind, "reachable": False, "runs": None, "fresh": False,
                "error": f"unsupported store scheme {kind}: storage_url must be a postgresql:// or sqlite:/// URL"}
    if not postgres and not _sqlite_store_file(url).exists():
        return {"kind": kind, "reachable": False, "error": _NO_STORE_YET, "runs": None, "fresh": True}
    try:
        with contextlib.closing(store_dialect.connect_readonly_url(_with_connect_timeout(url) if postgres else url)) as conn:
            runs = int(conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"])
    except Exception as exc:  # any driver failure is the answer this row reports
        error = _store_error(exc)
        return {"kind": kind, "reachable": False, "error": error, "runs": None, "fresh": error == _NO_RUNS_TABLE}
    return {"kind": kind, "reachable": True, "error": None, "runs": runs, "fresh": False}


def _workspace_facts(workspace_dir: str) -> dict:
    ws = Path(workspace_dir).expanduser()
    return {"workspace_dirs": {name: (ws / name).exists() for name in ("work", "runs", "intake")}}


def _git_and_forge_facts(profile: dict) -> dict:
    """`git_version`, the profile's forge name, whether that forge is installed,
    and `gh_auth` only for the github forge. Gathered with or without a profile."""
    try:
        done = subprocess.run(["git", "--version"], capture_output=True, text=True)
        git_version = done.stdout.strip() if done.returncode == 0 else None
    except OSError:
        git_version = None
    name = forge.forge_name(profile)
    facts = {"git_version": git_version or None, "forge": name, "forge_found": forge.forge_for(name) is not None}
    if name == "github":
        try:
            facts["gh_auth"] = route_sync_gh.auth_ok(subprocess.run)
        except OSError:
            facts["gh_auth"] = False
    return facts


_TOOLS_PLUGIN_GROUPS = ("coxswain.sources", "coxswain.forges", "coxswain.trackers")


def _tools_plugin_facts() -> dict:
    """Entry-point names per group, read in tools' own environment. Listing only."""
    return {g: sorted(ep.name for ep in importlib.metadata.entry_points(group=g)) for g in _TOOLS_PLUGIN_GROUPS}


def _doctor_profile(text: str | None) -> dict | None:
    """The doctor's one parse rule: a missing or unparseable profile is None, never a refusal."""
    try:
        return route.parse_profile(text) if text is not None else None
    except route.ProfileError:
        return None


def _gather_doctor_facts(profile_path: Path, repo: Path) -> dict:
    """Gathers exactly the Facts keys `doctor.checks` reads; never refuses on
    a missing or unparseable profile, since reporting that is the doctor's
    job (unlike `_resolve_profile_or_refuse`, which is for `file`/`launch`)."""
    text = _read_text_or_none(profile_path)
    facts: dict = {"profile_path": str(profile_path), "profile_text": text}
    profile = _doctor_profile(text)
    facts.update(_git_and_forge_facts(profile or {}))
    facts["plugins_tools"] = _tools_plugin_facts()
    if profile is None:
        return facts
    singles = [profile.get(k, "") for k in ("cartridges_dir", "provider_profile", "harness_dir", "workspace_dir")]
    roots = list(profile.get("skills_roots") or [])
    # Every profile path is expanded ONCE, here; `paths_exist` keeps the raw
    # strings as keys (doctor.py's contract) but tests the expanded path.
    expand = lambda p: str(Path(p).expanduser()) if p else ""  # noqa: E731
    facts["paths_exist"] = {p: Path(expand(p)).exists() for p in (*singles, *roots) if p}
    harness_dir = expand(profile.get("harness_dir", ""))
    venv_python = Path(harness_dir) / ".venv" / "bin" / "python" if harness_dir else Path("/nonexistent")
    facts["harness_python_exists"] = venv_python.exists()
    if facts["harness_python_exists"]:
        overlay_text = _read_text_or_none(repo / ".agent" / "cartridge.yaml")
        facts.update(_run_core_probe(str(venv_python), expand(profile.get("cartridges_dir", "")), profile.get("team", ""),
                                     [expand(r) for r in roots], raw_roots=roots, overlay_text=overlay_text))
    if profile.get("provider_profile"):
        facts.update(_provider_facts(expand(profile["provider_profile"])))
        if profile.get("workspace_dir"):
            facts["store"] = _store_facts(expand(profile["provider_profile"]), expand(profile["workspace_dir"]))
    if profile.get("workspace_dir"):
        facts.update(_workspace_facts(expand(profile["workspace_dir"])))
    facts["schema_versions"] = _schema_versions(harness_dir, expand(profile.get("provider_profile", "")),
                                                 [expand(r) for r in roots])
    return facts


def _parquet_traces_line(profile: dict | None) -> str | None:
    """Edge: the Parquet readability line for this profile's traces root, or None when
    the profile names no workspace_dir and so no root can be resolved."""
    if not profile or not profile.get("workspace_dir"):
        return None
    provider = profile.get("provider_profile")
    provider_data = store_url.read_provider_profile(provider) if provider else {}
    root = store_url.profile_traces_root(provider_data, Path(profile["workspace_dir"]).expanduser() / "runs")
    check = run_store.parquet_readable(root, run_store.harness_python(profile))
    endpoint = root.object_store.get("endpoint") if root.object_store else None
    return doctor.parquet_line(check.readable, check.reason, str(endpoint) if endpoint else None)


def _profile_lane_hosts(text: str) -> tuple[lane_hosts.LaneHost, ...] | lane_hosts.LaneHostError:
    """Reads `lane_hosts` straight from the YAML text; `route.parse_profile` only skips the block."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return lane_hosts.LaneHostError(f"profile is not readable as YAML: {exc}")
    return lane_hosts.parse_lane_hosts(data if isinstance(data, Mapping) else {})


def _run_ssh(argv: list[str]) -> tuple[int, str]:
    """Edge: the one real subprocess call for `setup doctor --host`; stderr joins the output rows."""
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: {exc}"
    return done.returncode, done.stdout


def _setup_doctor_host(a: argparse.Namespace, run) -> int:
    """Prints the named lane host's doctor rows under a header; non-zero when the host is unknown or its doctor fails."""
    path = _profile_path(a)
    text = _read_text_or_none(path)
    if text is None:
        print(f"cannot read profile: {path}")
        return 1
    hosts = _profile_lane_hosts(text)
    if isinstance(hosts, lane_hosts.LaneHostError):
        print(hosts.message)
        return 1
    host = lane_hosts.find_lane_host(hosts, a.host)
    if host is None:
        print(f"unknown lane host: {a.host}")
        print(f"configured: {', '.join(h.name for h in hosts) or 'none'}")
        return 1
    code, rows = remote_doctor.doctor_on_host(host, run)
    print(f"doctor on {host.name} ({host.ssh})")
    print("\n".join(rows))
    return code


def _setup_doctor(a: argparse.Namespace) -> int:
    if getattr(a, "host", None):
        return _setup_doctor_host(a, _run_ssh)
    repo = Path(a.repo).expanduser() if a.repo else Path.cwd()
    facts = _gather_doctor_facts(_profile_path(a), repo)
    rows = doctor.checks(facts)
    rc = doctor.exit_code(rows)
    # Informational: outside rows, so outside rc. Reuses the text the facts already read.
    parquet = _parquet_traces_line(_doctor_profile(facts["profile_text"]))
    if a.json:
        print(json.dumps({"rows": rows, "ok": rc == 0, "parquet_traces": parquet}, indent=2))
    else:
        print(doctor.render(rows))
        if parquet:
            print(parquet)
    return rc


def _install_facts(a: argparse.Namespace) -> dict:
    """Gathers exactly what `install_plan` needs and nothing it decides:
    PATH lookups, each repo's venv, the profile's existence, and the
    config/settings paths expanded once, here."""
    root = a.root
    config_dir = str(Path("~/.config").expanduser())
    claude_settings_path = str(Path("~/.claude/settings.json").expanduser())
    python_exists = {repo: (Path(root) / repo / ".venv" / "bin" / "python").exists()
                      for repo in setup_install.REPOS}
    return {
        "root": root,
        "team": a.team,
        "workspace": a.workspace,
        "provider_profile": a.provider_profile or f"{root}/coxswain-cartridges/providers/claude-code.yaml",
        "skills_root": a.skills_root or f"{root}/coxswain-cartridges/skills-plugins",
        "uv_on_path": shutil.which("uv") is not None,
        "python_exists": python_exists,
        "claude_on_path": shutil.which("claude") is not None,
        "profile_exists": Path(setup_install.profile_path(config_dir)).exists(),
        "force_profile": a.force_profile,
        "plugins": a.plugins,
        "hook": a.hook,
        "config_dir": config_dir,
        "claude_settings_path": claude_settings_path,
        "assume": a.assume,
        "window_ceiling_usd": a.window_ceiling_usd,
        "weekly_ceiling_usd": a.weekly_ceiling_usd,
    }


def _install_run(step: dict) -> str | None:
    argv = " ".join(step["argv"])
    try:
        proc = subprocess.run(step["argv"], cwd=step.get("cwd"), capture_output=True, text=True)
    except OSError as exc:
        print(f"FAILED: {argv}: {exc}")
        return f"{argv}: {exc}"
    if proc.returncode == 0:
        print(f"run: {argv}")
        return None
    tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
    if step.get("warn_only"):
        print(f"warn: {argv} exited {proc.returncode}: {tail}")
        return None
    print(f"FAILED: {argv} exited {proc.returncode}")
    print(tail)
    return f"{argv} exited {proc.returncode}"


def _execute_step(step: dict) -> str | None:
    op = step["op"]
    if op == "run":
        return _install_run(step)
    if op == "write":
        path = Path(step["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step["text"], encoding="utf-8")
        print(f"write {path}")
        return None
    if op == "skip":
        print(f"skip {step['what']} — {step['why']}")
        return None
    if op == "hook":
        path = Path(step["path"])
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError as exc:
            print(f"FAILED: {path} is not valid JSON: {exc}")
            return f"{path} is not valid JSON: {exc}"
        new_settings, changed = setup_install.hook_settings(existing)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_settings, indent=2) + "\n", encoding="utf-8")
            print(f"hook added to {path}")
        else:
            print(f"hook already present in {path}")
        return None
    if op == "print":
        print(step["text"])
        return None
    return f"unknown step op: {op!r}"


def _setup_install(a: argparse.Namespace) -> int:
    try:
        steps = setup_install.install_plan(**_install_facts(a))
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    if a.dry_run:
        print(setup_install.render_plan(steps))
        return 0
    for step in steps:
        failure = _execute_step(step)
        if failure is not None:
            return 1
    return 0


def _setup_fields(a: argparse.Namespace) -> tuple[str, str, str]:
    """(root, team, workspace) to prefill the TUI with, from the profile
    when it resolves, else empty strings — never a hard refusal here."""
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return "", "", ""
    try:
        profile = route.parse_profile(text)
    except route.ProfileError:
        return "", "", ""
    harness_dir = profile.get("harness_dir", "")
    root = str(Path(harness_dir).parent) if harness_dir else ""
    return root, profile.get("team", ""), profile.get("workspace_dir", "")


def _load_manifest(path: Path) -> dict | None:
    """The parsed manifest, or None on a missing or unparseable file — never
    a traceback; the caller turns None into a named, exit-2 refusal."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _checkout_facts(path: Path, fetch: bool = False) -> dict:
    """A component's checkout state. Present only when `<path>/.git`
    exists: `git -C` walks up to the nearest enclosing repository, so a
    bare directory — or one merely nested inside some other checkout —
    must never borrow that repository's tag or dirty state."""
    if not (path / ".git").exists():
        return {"present": False, "tag": None, "dirty": False}
    describe = subprocess.run(["git", "-C", str(path), "describe", "--tags", "--exact-match"],
                               capture_output=True, text=True)
    tag = describe.stdout.strip() if describe.returncode == 0 else None
    status = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True)
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)

    branch = git("branch", "--show-current").stdout.strip()
    fresh = not fetch or (bool(branch) and git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}").returncode == 0)
    tip = git("rev-parse", f"origin/{branch}")
    at_tip = bool(branch) and fresh and tip.returncode == 0 and tip.stdout.strip() == git("rev-parse", "HEAD").stdout.strip()
    return {"present": True, "tag": tag, "dirty": bool(status.stdout.strip()), "branch": branch or None,
            "branch_tip": at_tip}


def _gather_checkout_facts(root: Path, components: dict, fetch: bool = False) -> dict:
    """Every manifest component, plus any other git checkout actually
    present under `root` — the `extra` rows `install.rows` can then report.
    `fetch` refreshes each declared checkout's remote branch first (edge)."""
    options = {"fetch": True} if fetch else {}
    declared = {name: _checkout_facts(root / name, **options) for name in components}
    if not root.exists():
        return declared
    undeclared = {p.name for p in root.iterdir()
                  if p.is_dir() and p.name not in components and (p / ".git").exists()}
    return {**declared, **{name: _checkout_facts(root / name) for name in undeclared}}


def _manifest_provider_command(manifest: dict, provider: str) -> str:
    """The executable to look up on PATH: the manifest's own `command` for
    this provider when it names one, else the provider key itself."""
    return manifest.get("providers", {}).get(provider, {}).get("command", provider)


def _real_run(argv: list, cwd: str | None) -> tuple:
    """The subprocess wrapper `install_exec.execute` calls at the edge;
    stdout and stderr are folded together since the caller only prints."""
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def _manifest_at_version(manifest: dict, to: str | None) -> dict:
    """The manifest as given, unless `to` names a version: then every
    component's `tag` is overridden to `to`, so `--to VERSION` reaches
    `install.plan` as the pin every component is judged and cloned
    against, rather than being accepted and silently ignored."""
    if to is None:
        return manifest
    components = {name: {**spec, "tag": to} for name, spec in manifest.get("components", {}).items()}
    return {**manifest, "components": components}


def _install_execute(steps: list, manifest: dict, options: dict, root: Path) -> int:
    """Runs a planned install/upgrade for real: enrich the planner's steps
    with the repo/tag/team/workspace `install_exec` needs, execute them,
    report one ok/FAILED/skipped/refused line per step, then the versions
    table built from checkouts gathered again after execution — the facts
    gathered before a single step ran are not evidence of what landed.
    A `refuse` step is reported as `refused`, not `FAILED` — it never
    called `run` and its non-zero exit is a decision, not a command that
    broke. Exits 0 only when every step that ran, ran clean."""
    exec_steps = install_exec.from_plan(steps, manifest=manifest, options=options)
    results = install_exec.execute(exec_steps, root=str(root), run=_real_run)
    for result in results:
        step = result["step"]
        label = f"{step['kind']} {step.get('component', '')}".strip()
        if step["kind"] == "skip" or result["exit"] is None:
            print(f"skipped: {label}")
        elif step["kind"] == "refuse":
            print(f"refused: {label}: {result['output']}")
        elif result["exit"] == 0:
            print(f"ok: {label}")
        else:
            print(f"FAILED: {label}")
            for line in (result["output"] or "").rstrip().splitlines():
                print(f"    {line}")
    post_facts = {"root": str(root), "checkouts": _gather_checkout_facts(root, manifest.get("components", {}))}
    display = [{"component": c, "pinned_tag": p or "", "installed_tag": i or "", "status": s}
               for c, p, i, s in install.rows(manifest, post_facts, options.get("channel", "release"))]
    print(records.format_table(display, ["component", "pinned_tag", "installed_tag", "status"]))
    return 0 if all(result["exit"] in (0, None) for result in results) else 2


_NO_MANIFEST = "refusing: no manifest at {}; run `cox install` to fetch the components, or pass --manifest"


def _install(a: argparse.Namespace) -> int:
    manifest_path = Path(a.manifest) if a.manifest else Path(a.root) / "coxswain" / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(_NO_MANIFEST.format(manifest_path))
        return 2
    root = Path(a.root)
    channel = "edge" if a.edge else "release"
    facts = {
        "root": str(root),
        "checkouts": _gather_checkout_facts(root, manifest.get("components", {}), fetch=a.edge),
        "provider_cli_on_path": shutil.which(_manifest_provider_command(manifest, a.provider)) is not None,
    }
    options = {"provider": a.provider, "with": a.with_ or [], "root": str(root), "team": a.team,
               "workspace": a.workspace, "channel": channel}
    schema_state, schema_detail = schema.status(_schema_versions(str(root / "harness"), str(root / "cartridges")))
    if schema_state != "ok":
        print(f"schema  WARN  {schema_detail}")
    steps = install.plan(manifest, facts, options)
    print(f"channel: {channel}")
    for step in steps:
        print(f"{step['kind']} {step['component']}: {step['detail']}")
    if a.dry_run:
        return 2 if any(step["kind"] == "refuse" for step in steps) else 0
    return _install_execute(steps, manifest, options, root)


def _upgrade(a: argparse.Namespace) -> int:
    manifest_path = Path(a.manifest) if a.manifest else Path(a.root) / "coxswain" / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(_NO_MANIFEST.format(manifest_path))
        return 2
    manifest = _manifest_at_version(manifest, a.to)
    root = Path(a.root)
    facts = {
        "root": str(root),
        "checkouts": _gather_checkout_facts(root, manifest.get("components", {})),
        "provider_cli_on_path": shutil.which(_manifest_provider_command(manifest, a.provider)) is not None,
    }
    dirty = sorted(name for name, checkout in facts["checkouts"].items() if checkout.get("dirty"))
    if dirty:
        print(f"refusing: {root / dirty[0]} is dirty; refusing to upgrade it")
        return 2
    options = {"provider": a.provider, "with": a.with_ or [], "root": str(root), "team": a.team, "workspace": a.workspace}
    steps = install.plan(manifest, facts, options)
    for step in steps:
        print(f"{step['kind']} {step['component']}: {step['detail']}")
    if a.dry_run:
        return 2 if any(step["kind"] == "refuse" for step in steps) else 0
    return _install_execute(steps, manifest, options, root)


def _schema_versions(harness_dir: str = "", provider_profile: str = "", skills_roots: list[str] | None = None
                      ) -> dict[str, str | None]:
    """The schema each of the three tools was built against, read from the
    profile's (or `--root`'s) checkouts rather than imported, since graphs
    and cartridges are not installed in this venv."""
    return {
        "cartridges": schema.cartridges_schema(provider_profile, skills_roots or []),
        "graphs": schema.graphs_schema(harness_dir),
        "tools": schema.TOOLS_SCHEMA,
    }


def _versions(a: argparse.Namespace) -> int:
    manifest_path = Path(a.manifest) if a.manifest else Path(a.root or ".") / "coxswain" / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(_NO_MANIFEST.format(manifest_path))
        return 2
    root = Path(a.root) if a.root else manifest_path.resolve().parent.parent
    facts = {"root": str(root), "checkouts": _gather_checkout_facts(root, manifest.get("components", {})),
              "provider_cli_on_path": False}
    schema_versions = _schema_versions(str(root / "harness"), str(root / "cartridges"))
    display = [{"component": c, "pinned_tag": p or "", "installed_tag": i or "", "status": s,
                "schema": schema.cell(schema_versions.get(c)) if c in schema_versions else "?"}
               for c, p, i, s in install.rows(manifest, facts)]
    print(records.format_table(display, ["component", "pinned_tag", "installed_tag", "status", "schema"]))
    return 0


def _home(a: argparse.Namespace) -> int:
    from agent_tools import home_screen
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    workspace = runs_dir.parent
    plugin_root = _plugin_root(profile.get("skills_roots") or [])
    return home_screen.main(
        runs_dir, workspace / "work", workspace / "intake", str(plugin_root or ""),
        window_ceiling_usd=profile.get("window_ceiling_usd"),
    )


def _setup_tui(a: argparse.Namespace) -> int:
    if not sys.stdin.isatty():
        print("setup: needs a terminal; use setup doctor / setup install / cartridge init directly")
        return 2
    return setup_screen.main(*_setup_fields(a))


_USAGE_ASSESS_EXIT = {"go": 0, "go_degraded": 0, "hold": 3, "stop": 4}

_GATE_LEVELS = ("ticket", "phase", "epic", "full")


def _resolved_gate_level(runs_dir: Path) -> str:
    """`level` from `<runs_dir>/policy.gate.json`; `ticket` when absent, unreadable or unknown, never `full`."""
    text = _read_text_or_none(runs_dir / "policy.gate.json")
    try:
        raw = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        raw = None
    level = raw.get("level") if isinstance(raw, dict) else None
    return level if level in _GATE_LEVELS else "ticket"


def _resolved_pacing_policy(runs_dir: Path) -> pacing.Policy:
    """The cartridge's resolved `policy.pacing`, read from
    `<runs_dir>/policy.pacing.json` when a landing step has dropped one there;
    the unmeasured-and-uncapped `usage_window.DEFAULT_POLICY` when the file is
    absent, unreadable, or not a mapping — same skip-not-raise contract as
    `records.ceiling_for`'s own `<run_id>.ceiling.json` handling. A key the
    file omits falls back to the matching `DEFAULT_POLICY` field, not to a
    guess. The file's `max_in_flight` is not a `Policy` field: `chair run`
    reads it as its lane cap after the team cartridge's (`_chair_max_in_flight`, 3 when absent)."""
    default = usage_window.DEFAULT_POLICY
    text = _read_text_or_none(runs_dir / "policy.pacing.json")
    if text is None:
        return default
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return default
    if not isinstance(raw, dict):
        return default
    return pacing.Policy(
        pace_thresholds=tuple(raw.get("pace_thresholds", default.pace_thresholds)),
        tier_ladder=tuple(raw.get("tier_ladder", default.tier_ladder)),
        effort_ladder=tuple(raw.get("effort_ladder", default.effort_ladder)),
        min_headroom_usd=float(raw.get("min_headroom_usd", default.min_headroom_usd)),
        hard_stop_fraction=float(raw.get("hard_stop_fraction", default.hard_stop_fraction)),
        weekly_hard_stop_fraction=float(raw.get("weekly_hard_stop_fraction", default.weekly_hard_stop_fraction)),
    )


def _usage_assessment(
    runs_dir, window_ceiling_usd: float | None = None, weekly_ceiling_usd: float | None = None,
    weekly_reset: str | None = None,
) -> pacing.Assessment:
    """Computed once via the gatherer and shared by every surface that
    narrates it: `usage assess`, `route context`'s docket line, and `route
    launch`'s gate all call this so the same window yields the same reason.
    """
    now = datetime.datetime.now(datetime.UTC)
    reset = usage_window.parse_weekly_reset(weekly_reset)
    usage = usage_window.read_usage(runs_dir, now, reset)
    window = usage_window.gather(runs_dir, now, ceiling_usd=window_ceiling_usd, usage=usage)
    weekly = usage_window.gather_weekly(runs_dir, now, weekly_ceiling_usd, usage=usage, reset=reset)
    policy = _resolved_pacing_policy(Path(runs_dir))
    return pacing.assess(window, policy, now, weekly=weekly)


def _usage_assess(a: argparse.Namespace) -> int:
    # Resolved the same tolerant way `_route_context` resolves its profile:
    # a missing or unreadable profile just means no ceiling, not a raise.
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        profile = {}
    result = _usage_assessment(
        a.runs_dir, profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd"), profile.get("weekly_reset"),
    )
    if a.json:
        d = dataclasses.asdict(result)
        d["hold_until"] = result.hold_until.isoformat() if result.hold_until else None
        print(json.dumps(d, indent=2))
    else:
        print(f"{result.verdict}: {result.reason}")
    return _USAGE_ASSESS_EXIT[result.verdict]


def _bare_group(parser: argparse.ArgumentParser):
    """Default `fn` for a group parser whose subcommand is optional: an
    operator who runs the group alone sees that group's own help and a exit
    code of 2, not a traceback or silence."""
    def _fn(_a: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _fn


RUNS_GROUP = commands.Group(
    name="runs", help="what a harness run recorded, and cleaning up after it",
    description="What a harness run recorded, and cleaning up after it.",
    epilog="examples:\n  cox runs fetch <run>\n  cox runs land <run> --repo PATH --apply\n  cox runs recover <run> <task> --repo PATH\n"
           "  cox runs usage <run> --json\n  cox runs detail <run> --json",
)
RUNS_COMMANDS = [
    commands.Command(
        "usage", "runs", "usage stats and cost for one run",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        _runs_usage, False, (),
    ),
    commands.Command(
        "trace", "runs", "the tool-call trace for one run",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--role",)), commands.Arg(("-v", "--verbose"), {"action": "store_true"})),
        _runs_trace, False, (),
    ),
    commands.Command(
        "clean", "runs", "delete a run's worktree and branches locally",
        (
            commands.Arg(("run_id",)), commands.Arg(("--repo",), {"required": True}), commands.Arg(("--worktree-root",), {"default": "~/worktrees"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--apply",), {"action": "store_true"}), commands.Arg(("--force",), {"action": "store_true", "help": "delete every branch regardless of whether its task is on main"}),
        ),
        _runs_clean, False, (),
    ),
    commands.Command(
        "land", "runs", "merge a run's branch into the target repo",
        (
            commands.Arg(("run_id",)), commands.Arg(("--repo",), {"required": True}), commands.Arg(("--task",)),
            commands.Arg(("--phase",), {"help": "land the whole phase off its own epic branch instead of one task"}), commands.Arg(("--label",)),
            commands.Arg(("--force",), {"action": "store_true", "help": "land despite a foreign live leader"}),
            commands.Arg(("--no-claim",), {"action": "store_true", "help": "land without taking an unheld or stale loop"}),
            commands.Arg(("--worktree-root",), {"default": "~/worktrees"}), commands.Arg(("--apply",), {"action": "store_true"}), commands.Arg(("--no-merge",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--gate",), {"choices": ["ticket", "phase", "epic", "full"], "help": "override the resolved gate level for this invocation"}),
        ),
        _runs_land, False, (),
    ),
    commands.Command(
        "fetch", "runs", "pull an ended remote lane's run directory, log and branches to this machine",
        (
            commands.Arg(("--runs-dir",), {"help": "override: resolve the run's records here instead of the profile's workspace_dir"}),
            commands.Arg(("--profile",)),
            # Not an argparse group: Python 3.14 prints a required group holding a positional as `(--all | run_id)`
            # where 3.12 prints `[--all] [run_id]`, so the help could not read the same on both. _runs_fetch checks it.
            commands.Arg(("run_id",), {"nargs": "?"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "fetch every remote run with no local tasks directory yet; a live run is skipped"}),
        ),
        _runs_fetch, False, (),
    ),
    commands.Command(
        "review", "runs", "review a contributor PR with the review graph and post the verdict",
        (commands.Arg(("--pr",), {"required": True, "help": "https://github.com/<owner>/<repo>/pull/<n>"}), commands.Arg(("--profile",))),
        _runs_review, False, (),
    ),
    commands.Command(
        "recover", "runs", "merge an approved task's commit into its phase branch after an escalated merge",
        (
            commands.Arg(("run_id",)), commands.Arg(("task_id",)), commands.Arg(("--repo",), {"required": True}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the merge that would be made and exit without touching anything"}),
        ),
        _runs_recover, False, (),
    ),
    commands.Command(
        "series", "runs", "per-run summary rows across a runs directory",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"}), commands.Arg(("--append",))),
        _runs_series, False, (),
    ),
    commands.Command(
        "wait", "runs", "block until a busy lane's run exits, then print its outcome lines",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--max-seconds",), {"type": float, "default": 3600}),
            commands.Arg(("--interval",), {"type": float, "default": 15}), commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _runs_wait, False, (),
    ),
    commands.Command(
        "events", "runs", "poll a run's log for structured events",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--follow",), {"action": "store_true"}), commands.Arg(("--json",), {"action": "store_true"})),
        _runs_events, False, (),
    ),
    commands.Command(
        "top", "runs", "live table of busy lanes (runs in flight); --once prints it and exits",
        (commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--interval",), {"type": float, "default": 3}), commands.Arg(("--once",), {"action": "store_true"})),
        _runs_top, False, (),
    ),
    commands.Command(
        "bar", "runs", "one Waybar JSON line: busy lanes, cost, class idle|running|attention",
        (commands.Arg(("--runs-dir",), {"default": "runs"}),),
        _runs_bar, False, (),
    ),
    commands.Command(
        "notify", "runs", "desktop notifications for exits, quarantines, budget stops and cost",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--once",), {"action": "store_true"}), commands.Arg(("--interval",), {"type": float, "default": 10}),
            commands.Arg(("--replay",), {"action": "store_true", "help": "emit history on first start; default is silent for existing runs when no state file is present"}),
        ),
        _runs_notify, False, (),
    ),
    commands.Command(
        "detail", "runs", "one run's timeline, objection and last tool calls",
        (commands.Arg(("run_id",)), commands.Arg(("--runs-dir",), {"default": "runs"}), commands.Arg(("--json",), {"action": "store_true"})),
        _runs_detail, False, (),
    ),
    commands.Command(
        "stranded", "runs", "every approved task record whose work item is not done, with its remedy",
        (
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _runs_stranded, False, (),
    ),
    commands.Command(
        "cause", "runs", "record why one run's attempt at a task was quarantined",
        (
            commands.Arg(("run_id",)), commands.Arg(("task_id",)), commands.Arg(("cause",), {"choices": list(stats_chair.CAUSES)}),
            commands.Arg(("--note",), {"help": "free text kept beside the cause"}),
            commands.Arg(("--runs-dir",), {"help": "override: resolve task records here instead of the profile's workspace_dir"}), commands.Arg(("--profile",)),
        ),
        _runs_cause, False, (),
    ),
]

VERSIONS_GROUP = commands.Group(
    name="versions", help="component versions against the manifest", description="", epilog="",
    args=(commands.Arg(("--root",)), commands.Arg(("--manifest",))), fn=_versions,
)

INSTALL_GROUP = commands.Group(
    name="install", help="clone/update coxswain components against the manifest", description="", epilog="",
    args=(
        commands.Arg(("--root",), {"required": True}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--edge",), {"action": "store_true"}),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
    ),
    fn=_install,
)

UPGRADE_GROUP = commands.Group(
    name="upgrade", help="fetch and check out newer pinned versions; refuses dirty checkouts", description="", epilog="",
    args=(
        commands.Arg(("--root",), {"required": True}),
        commands.Arg(("--manifest",)),
        commands.Arg(("--provider",), {"default": "claude-code"}),
        commands.Arg(("--with",), {"action": "append", "default": None, "dest": "with_", "metavar": "FLAG"}),
        commands.Arg(("--team",)),
        commands.Arg(("--workspace",)),
        commands.Arg(("--to",)),
        commands.Arg(("--dry-run",), {"action": "store_true"}),
    ),
    fn=_upgrade,
)

HOME_GROUP = commands.Group(
    name="home", help="the live dashboard: runs, leader, backlog", description="", epilog="",
    args=(commands.Arg(("--profile",)),), fn=_home,
)

USAGE_GROUP = commands.Group(
    name="usage", help="spend pacing against the ceiling for the current window",
    description="Spend pacing against the ceiling for the current window.",
    epilog="examples:\n  cox usage assess\n  cox usage assess --json --runs-dir runs",
)
USAGE_COMMANDS = [
    commands.Command(
        "assess", "usage", "the pacing verdict for the current spend window",
        (
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the window ceiling (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        _usage_assess, False, (),
    ),
]

STATS_GROUP = commands.Group(
    name="stats", help="load the run corpus into the stats store",
    description="Load the run corpus into the stats store.",
    epilog="examples:\n  cox stats ingest\n  cox stats ingest runs --db workspace/stats/stats.db"
           "\n  cox stats roles --json\n  cox stats tiers --since 2026-09-01\n  cox stats gates --since 2026-09-01\n  cox stats explain build --json\n  cox stats series --json"
           "\n  cox stats coverage --json\n  cox stats bounds --json\n  cox stats spend-mix --json"
           "\n  cox stats examples --role handoff --out examples.jsonl"
           "\n  cox stats system-one --role handoff --propose",
)
STATS_COMMANDS = [
    commands.Command(
        "ingest", "stats", "load usage, task, node and launch records into stats.db",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--work-store-root",), {"default": "work"}),
            commands.Arg(("--cartridges-repo",), {"default": None}),
        ),
        _stats_ingest, False, (),
    ),
    commands.Command(
        "roles", "stats", "landed rate, attempts-to-land and $/landed per role and model",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--cartridge-sha",), {"default": None, "help": "keep only runs on this cartridge_sha"}),
            commands.Arg(("--provider-profile",), {"default": None, "help": "keep only runs on this provider_profile"}),
        ),
        _stats_roles, False, (),
    ),
    commands.Command(
        "tiers", "stats", "per-role model summaries, a cost-aware pick and the spend it would save",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--min-samples",), {"type": int, "default": 20, "help": "tasks a model needs before it can be picked (default 20)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_tiers, False, (),
    ),
    commands.Command(
        "causes", "stats", "quarantined attempts by cause and kind, with sample reasons",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_causes, False, (),
    ),
    commands.Command(
        "efficiency", "stats", "cost per turn, cost per landed task, first-try rate, waste share, per day",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--days",), {"type": int, "default": 7, "help": "UTC days to report, today included (default 7)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_efficiency, False, (),
    ),
    commands.Command(
        "gates", "stats", "what each review, validation and plan gate costs and how often it changes the outcome",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--store",), {"action": "store_true", "help": "read task and call rows from the run store instead of stats.db"}),
            commands.Arg(("--runs-dir",), {"default": "runs", "help": "the run store's directory, with --store"}),
            commands.Arg(("--min-sample",), {"type": int, "default": stats_gates.MIN_SAMPLE, "help": "decided tasks a role needs before it is judged, else 'not enough data' (default %(default)s)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_gates, False, (),
    ),
    commands.Command(
        "explain", "stats", "the failure-class breakdown behind one role",
        (
            commands.Arg(("role",)),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_explain, False, (),
    ),
    commands.Command(
        "series", "stats", "per-run summary rows read from the stats store",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--cartridge-sha",), {"default": None, "help": "keep only runs on this cartridge_sha"}),
            commands.Arg(("--provider-profile",), {"default": None, "help": "keep only runs on this provider_profile"}),
        ),
        _stats_series, False, (),
    ),
    commands.Command(
        "coverage", "stats", "known/total provenance rows for runs, calls and tasks",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_coverage, False, (),
    ),
    commands.Command(
        "bounds", "stats", "n/p50/p95/max and strict/moderate/liberal candidate ceilings per role and model",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--level",), {"choices": ("strict", "moderate", "liberal"), "default": None}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--write",), {"default": None, "help": "also write the JSON table to PATH inside the repo checkout"}),
        ),
        _stats_bounds, False, (),
    ),
    commands.Command(
        "spend-mix", "stats", "per-model token counts and cost share by class, plus the build-only split",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_spend_mix, False, (),
    ),
    commands.Command(
        "chair", "stats", "harness PRs and $, chair $ per PR, hand-finished lands and quarantine $ by cause",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only records and transcript lines dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--session",), {"action": "append", "default": None, "help": "a Claude session id to count as chair cost, beside those in chair.json (repeatable)"}),
            commands.Arg(("--work-store-root",), {"default": "work"}),
            commands.Arg(("--projects-dir",), {"default": "~/.claude/projects"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming cartridges_dir (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        _stats_chair, False, (),
    ),
    commands.Command(
        "examples", "stats", "the local system-one backend's training file (JSON lines) from task records",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"action": "append", "required": True, "help": "handoff or review_charter; repeat for several"}),
            commands.Arg(("--out",), {"default": None, "help": "write the file to PATH (default: stdout)"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only records dated on or after DATE (YYYY-MM-DD)"}),
        ),
        _stats_examples, False, (),
    ),
    commands.Command(
        "system-one", "stats", "shadow-to-on graduation report per role, and a proposal for the maintainer to approve",
        (
            commands.Arg(("runs_dir",), {"nargs": "?", "default": "runs"}),
            commands.Arg(("--role",), {"default": None, "help": "report this role only"}),
            commands.Arg(("--since",), {"default": None, "help": "keep only rows dated on or after DATE (YYYY-MM-DD)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--propose",), {"action": "store_true", "help": "write a graduation proposal for each READY role; never edits a profile"}),
            commands.Arg(("--plans-dir",), {"default": "plans", "help": "where --propose writes system-one-graduation-<role>-<date>.md"}),
        ),
        _stats_system_one, False, (),
    ),
    commands.Command(
        "lanes", "stats", "per hour, the average and peak busy lanes and the idle minutes, from the store's run spans",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--hours",), {"type": int, "default": 24, "help": "how many clock hours back to report"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _stats_lanes, False, (),
    ),
]


def _lake_provider(a: argparse.Namespace) -> tuple[Mapping, str | None]:
    """Edge. (provider profile, problem). A profile that is named but unusable is a problem, not a silent default lake.

    Only an unnamed profile falls back to files in the runs dir."""
    explicit = a.profile or os.environ.get("AGENT_TOOLS_PROFILE")
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return {}, f"no profile at {_profile_path(a)}" if explicit else None
    try:
        routing_profile = route.parse_profile(text)
    except route.ProfileError as exc:
        return {}, f"profile unreadable: {exc}"
    provider_path = routing_profile.get("provider_profile")
    if not provider_path:
        return {}, None
    provider_text = _read_text_or_none(Path(provider_path).expanduser())
    if provider_text is None:
        return {}, f"provider profile not readable: {provider_path}"
    provider = store_url.read_provider_profile(provider_path)
    has_content = any(line.strip() and not line.lstrip().startswith("#") for line in provider_text.splitlines())
    if has_content and not provider:
        return {}, f"provider profile is not a YAML mapping: {provider_path}"
    return provider, None


def _lake_sync_report(results: Sequence, traces: tuple[int, int, int], dry_run: bool, new_tables: Sequence[str],
                      catalog: str, warehouse: str, traces_root: str) -> dict:
    """`results` are `SyncResult`s and `traces` is (found, registered, skipped). Every URL is redacted."""
    found, registered, skipped = traces
    return {
        "dry_run": dry_run,
        "new_tables": list(new_tables),
        "catalog": lake_config.redact(catalog),
        "warehouse": lake_config.redact(warehouse),
        "traces_root": lake_config.redact(traces_root),
        "tables": [
            {"table": r.table, "rows_found": r.rows_found, "rows_appended": r.rows_appended, "mark": r.new_mark}
            for r in results
        ],
        "traces": {"found": found, "registered": registered, "skipped": skipped, "would_register": found - skipped},
    }


def _lake_sync_lines(report: dict) -> list[str]:
    """A dry run says what a real run would do; its mark is the one it would set."""
    dry = report["dry_run"]
    created = [f"lake: would create {', '.join(report['new_tables'])}"] if report["new_tables"] else []
    tables = [
        f"{t['table']}: would append {t['rows_found']} (mark {t['mark'] or 'none'})" if dry
        else f"{t['table']}: {t['rows_appended']} appended (mark {t['mark'] or 'none'})"
        for t in report["tables"]
    ]
    traces = report["traces"]
    trace_line = (
        f"traces: would register {traces['would_register']}, {traces['skipped']} skipped" if dry
        else f"traces: {traces['registered']} registered, {traces['skipped']} skipped"
    )
    return [*created, *tables, trace_line]


def _lake_catalog_missing(catalog_uri: str) -> bool:
    """True for a `sqlite:///` catalog whose file does not exist yet; any other catalog is assumed to exist."""
    return catalog_uri.startswith("sqlite:///") and not Path(catalog_uri.removeprefix("sqlite:///")).exists()


def _lake_real_run(config: lake_config.LakeConfig, store: str, root: str) -> Callable[[], dict]:
    """Edge. Create any missing table; return the callable a lease guards that appends the new rows and registers traces."""
    catalog = lake_config.load_catalog(config)
    # Imported here: these modules import pyiceberg and pyarrow at module top, and cli.py must load without the extra.
    from agent_tools import lake_sync, lake_tables, lake_traces

    lake_tables.ensure_tables(catalog)

    def sync() -> dict:
        results = lake_sync.sync(catalog, store)
        traces = lake_traces.register_traces(catalog, root)
        return _lake_sync_report(results, traces, False, (), config.catalog_uri, config.warehouse, root)

    return sync


def _lake_open_readonly(config: lake_config.LakeConfig, name: str):
    """Edge. The configured catalog opened so that it creates nothing; None when it holds no catalog tables yet."""
    from pyiceberg.catalog.sql import IcebergTables, SqlCatalog
    from sqlalchemy import inspect as sql_inspect

    # Measured: a SqlCatalog creates its SQLite file on first connect, and its catalog tables on construction on any backend.
    if _lake_catalog_missing(config.catalog_uri):
        return None
    catalog = SqlCatalog(name, uri=config.catalog_uri, warehouse=config.warehouse, init_catalog_tables="false")
    return catalog if sql_inspect(catalog.engine).has_table(IcebergTables.__tablename__) else None


def _lake_dry_run(config: lake_config.LakeConfig, store: str, root: str) -> dict:
    """Edge. What a real run would do, read from the lake without creating or writing anything.

    A table the lake lacks reads as empty: no mark, no data files."""
    # An in-memory catalog: it raises LakeUnavailable when the extra is missing, names the catalog, and touches no file.
    probe = lake_config.load_catalog(lake_config.LakeConfig("sqlite:///:memory:", config.warehouse))
    from pyiceberg.exceptions import NoSuchTableError
    from pyiceberg.io import load_file_io

    from agent_tools import lake_sync, lake_tables, lake_traces

    real = _lake_open_readonly(config, probe.name)
    names = [f"{lake_tables.NAMESPACE}.{name}" for name in lake_tables.TABLES]
    new_tables = names if real is None else [n for n in names if not real.table_exists(n)]
    # The three attributes a dry run reads from a table; tests pin that lake_sync and lake_traces read no others.
    absent = types.SimpleNamespace(
        properties={}, io=load_file_io(dict(probe.properties), root),
        scan=lambda: types.SimpleNamespace(plan_files=lambda: []),
    )

    def load_table(identifier: str):
        try:
            return absent if real is None else real.load_table(identifier)
        except NoSuchTableError:
            return absent

    # The one catalog method a dry run calls; tests pin that lake_sync and lake_traces call no other.
    preview = types.SimpleNamespace(load_table=load_table)
    results = lake_sync.sync(preview, store, dry_run=True)
    traces = lake_traces.register_traces(preview, root, dry_run=True)
    return _lake_sync_report(results, traces, True, new_tables, config.catalog_uri, config.warehouse, root)


def _lake_refuse(a: argparse.Namespace, message: str) -> int:
    print(json.dumps({"error": message}) if a.json else message)
    return 2


def _lake_sync_done(a: argparse.Namespace, report: dict) -> int:
    print(json.dumps(report, indent=2) if a.json else "\n".join(_lake_sync_lines(report)))
    return 0


def _lake_sync_outcome(a: argparse.Namespace, outcome: lake_lease.SyncOutcome) -> int:
    """Report a real run's outcome once the lease decided whether it ran; a held or errored sync still exits 0."""
    if isinstance(outcome, lake_lease.Synced):
        return _lake_sync_done(a, outcome.value)
    if isinstance(outcome, lake_lease.SkippedHeld):
        message = f"lake: sync skipped (held by {outcome.holder})"
        print(json.dumps({"status": "held", "holder": outcome.holder}, indent=2) if a.json else message)
        return 0
    message = f"lake: sync skipped ({outcome.name})"
    print(json.dumps({"status": outcome.name}, indent=2) if a.json else message)
    return 0


def _lake_sync(a: argparse.Namespace) -> int:
    """Edge. Sync the run store and the trace files into the Iceberg lake; `--dry-run` creates and writes nothing."""
    # Absolute: `file://runs/lake` would name a host, and a relative trace path registered once would break from another cwd.
    runs_dir = Path(a.runs_dir).resolve()
    provider, problem = _lake_provider(a)
    if problem:
        return _lake_refuse(a, f"lake: {problem}")
    config = lake_config.resolve_lake(provider, runs_dir)
    store = store_url.profile_store_url(provider, runs_dir)
    root = store_url.profile_traces_root(provider, runs_dir).url
    root = root if "://" in root else str(Path(root).resolve())
    try:
        if a.dry_run:
            return _lake_sync_done(a, _lake_dry_run(config, store, root))
        outcome = lake_lease.sync_under_lease(_lake_real_run(config, store, root), runs_dir)
    except lake_config.LakeUnavailable as err:
        return _lake_refuse(a, str(err))
    except ImportError as err:
        # pyiceberg imported, but a module the lake also needs, such as pyarrow, did not.
        return _lake_refuse(a, f"the Iceberg lake needs the optional extra `lake`; {err.name or err} is missing: "
                               "pip install 'coxswain-tools[lake]'")
    return _lake_sync_outcome(a, outcome)


def _lake_config_for(a: argparse.Namespace) -> tuple[lake_config.LakeConfig | None, str | None]:
    """Edge. (lake config, problem) for a read command; the same resolution `cox lake sync` uses."""
    provider, problem = _lake_provider(a)
    if problem:
        return None, f"lake: {problem}"
    return lake_config.resolve_lake(provider, Path(a.runs_dir).resolve()), None


def _lake_extra_missing(a: argparse.Namespace, err: ImportError) -> int:
    # pyiceberg or a module the lake also needs, such as duckdb or pyarrow, did not import.
    return _lake_refuse(a, f"the Iceberg lake needs the optional extra `lake`; {err.name or err} is missing: "
                           "pip install 'coxswain-tools[lake]'")


def _lake_query(a: argparse.Namespace) -> int:
    """Edge. Run DuckDB SQL over the lake tables and print the rows as a table, or as JSON with `--json`."""
    config, problem = _lake_config_for(a)
    if config is None:
        return _lake_refuse(a, problem or "lake: no config")
    try:
        import duckdb

        from agent_tools import lake_query

        columns, rows = lake_query.query(lake_config.load_catalog(config), a.sql)
    except lake_config.LakeUnavailable as err:
        return _lake_refuse(a, str(err))
    except ImportError as err:
        return _lake_extra_missing(a, err)
    except duckdb.Error as err:
        return _lake_refuse(a, str(err))
    print(json.dumps(lake_query.rows_to_json(columns, rows), indent=2) if a.json else lake_query.render_table(columns, rows))
    return 0


def _lake_doctor(a: argparse.Namespace) -> int:
    """Edge. One line per lake check; exit 1 when the verdict is fail."""
    config, problem = _lake_config_for(a)
    if config is None:
        return _lake_refuse(a, problem or "lake: no config")
    try:
        from agent_tools import lake_doctor
    except ImportError as err:
        return _lake_extra_missing(a, err)
    checks = lake_doctor.run_checks(config)
    verdict = lake_doctor.verdict(checks)
    if a.json:
        print(json.dumps({"verdict": verdict, "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]}, indent=2))
    else:
        print("\n".join([*(f"{c.status} {c.name}: {c.detail}" for c in checks), f"verdict: {verdict}"]))
    return 1 if verdict == lake_doctor.FAIL else 0


LAKE_GROUP = commands.Group(
    name="lake", help="the Iceberg lake: sync the run store and traces into it, query it, check it",
    description="The Iceberg lake: sync the run store and traces into it, query it with SQL, and check it. Needs the optional extra `lake`.",
    epilog="examples:\n  cox lake sync\n  cox lake sync --dry-run --json\n"
           "  cox lake query \"SELECT count(*) AS n FROM runs\"\n  cox lake doctor",
)
LAKE_COMMANDS = [
    commands.Command(
        "sync", "lake", "append new run-store rows and register new trace files; --dry-run writes nothing",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "report what would be synced; create and write nothing"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _lake_sync, False, (),
    ),
    commands.Command(
        "query", "lake", "run DuckDB SQL over the lake tables (runs, phases, ...) and print the rows",
        (
            commands.Arg(("sql",), {"help": "the SQL to run; a lake table is named by its bare name, such as runs"}),
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _lake_query, False, (),
    ),
    commands.Command(
        "doctor", "lake", "check the lake: catalog, namespace, warehouse and each table; exits 1 on a failed check",
        (
            commands.Arg(("--runs-dir",), {"default": "runs"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _lake_doctor, False, (),
    ),
]


_DEV_MOVED = "moved: run `uv run --frozen python -m devtools <command> ...` from the coxswain checkout (from 0.15.0)"


def _dev_moved(a: argparse.Namespace) -> int:
    print(_DEV_MOVED)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cox",
        description=__doc__,
        epilog=(
            "examples:\n"
            "  bare cox opens the coxswain session\n"
            "  cox setup doctor checks this machine\n"
            "  cox route launch epic runs a filed initiative"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # dest="launcher_profile", not "profile": eight subparsers below declare
    # their own `--profile` with default None, and argparse copies a matched
    # subparser's namespace back over the parent's, so a shared dest would
    # let `cox --profile P route status` silently lose P to that default.
    p.add_argument("--profile", dest="launcher_profile", help="bare cox: the profile to launch claude against")
    p.add_argument("--no-plugin", action="store_true", help="bare cox: start claude without --plugin-dir")
    p.add_argument("--print-argv", action="store_true", help="bare cox: print the claude argv and cwd instead of exec'ing it")
    sub = p.add_subparsers(dest="group", required=False)

    group, rows = _table_entry("runs")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("stats")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("lake")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("usage")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("router")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("steward")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("epic")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("plan")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("route")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("chair")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("host")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("courier")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("install")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("upgrade")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("versions")
    commands.build_parser(rows, [group], sub)

    dev = sub.add_parser("dev", help="moved: maintainer commands now run from the coxswain checkout",
                         description=_DEV_MOVED)
    # swallow every subcommand and flag the old group took, so the pointer prints instead of argparse erroring
    dev.add_argument("rest", nargs=argparse.REMAINDER)
    dev.set_defaults(fn=_dev_moved)

    old_rel = sub.add_parser("release", help=argparse.SUPPRESS)
    old_rel.add_argument("rest", nargs=argparse.REMAINDER)
    old_rel.set_defaults(fn=_dev_moved)

    group, rows = _table_entry("home")
    commands.build_parser(rows, [group], sub)

    group, rows = _table_entry("setup")
    setup_p = commands.build_parser(rows, [group], sub)["setup"]
    # commands.build_parser's generic bare-group fallback prints help and
    # exits 2; a bare `cox setup` instead opens the TUI, so override it here.
    setup_p.set_defaults(fn=_setup_tui)
    return p


def _plugin_root(skills_roots, name: str = "coxswain"):
    """First skills root carrying `<root>/<name>/.claude-plugin/plugin.json`,
    or None if none of them do."""
    for root in skills_roots:
        candidate = Path(root).expanduser() / name
        if (candidate / ".claude-plugin" / "plugin.json").exists():
            return candidate
    return None


def _launcher_argv(plugin_root, skills_roots, no_plugin: bool, extra_args: list[str]):
    """spec §7: the argv for a bare `cox` — real Claude Code with the
    coxswain plugin loaded, unless `no_plugin` or no skills root carries one.
    Takes the already-resolved `plugin_root` (a Path, or None if none of
    `skills_roots` carries one) as data: no filesystem probe here, so this is
    testable with literals. `extra_args` are appended as given — the caller's
    own `--` already did the job of separating them from cox's own flags, so
    none is re-inserted here. Returns (argv, warning); warning is the
    one-line fallback notice, or None when a plugin was found or none was
    asked for."""
    plugin_flag = [] if no_plugin or plugin_root is None else ["--plugin-dir", str(plugin_root)]
    warning = None if no_plugin or plugin_root is not None else f"coxswain plugin not found under {skills_roots}; starting plain claude"
    return ["claude", *plugin_flag, *extra_args], warning


def _spawn(argv: list[str]) -> subprocess.Popen:
    """Detached: own session via `start_new_session`, stdio to devnull, so `argv` outlives the caller's later `execvp`."""
    return subprocess.Popen(
        argv, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _courier_workspace(a: argparse.Namespace) -> Path | None:
    profile, reason, *_ = _gather_context(_profile_path(a))
    if reason: print(f"routing: {reason}"); return None
    return Path(profile["workspace_dir"]).expanduser()


def _courier_send(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None: return 2
    ref = courier.parse_reference(a.ref)
    if ref is None or courier.resolve(ref, workspace) is None:
        print(f"courier: {a.ref} does not resolve"); return 2
    path = workspace / "courier.jsonl"
    sender = (chair.read(workspace / "runs") or {}).get("session") or "cli"
    entry = courier.send(ref, sender, a.to, a.note, uuid.uuid4().hex)
    path.write_text(courier.append_line(_read_text_or_none(path) or "", entry), encoding="utf-8")
    return 0


def _print_inbox(blob: str, label: str | None, holder: str | None) -> None:
    for entry in courier.inbox(blob, label, holder=holder):
        print(f"{entry['id']}: {entry['from']} -> {entry['to']}: {entry['note']} ({entry['ref']})")


def _lock_holder_or_none(runs_dir: Path) -> str | None:
    """A missing, unreadable, or torn lock file has no holder; it does not refuse
    a read-only listing (cf. `runs_detail_screen.facts_for`'s same degrade)."""
    try:
        return (chair.read(runs_dir) or {}).get("session")
    except (OSError, json.JSONDecodeError):
        return None


def _courier_inbox(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None: return 2
    holder = _lock_holder_or_none(workspace / "runs")
    _print_inbox(_read_text_or_none(workspace / "courier.jsonl") or "", a.label, holder)
    return 0


def _courier_ack(a: argparse.Namespace) -> int:
    workspace = _courier_workspace(a)
    if workspace is None: return 2
    path = workspace / "courier.jsonl"
    blob = _read_text_or_none(path) or ""
    updated = courier.ack(blob, a.id)
    if updated == blob: print(f"courier: no entry {a.id}"); return 2
    path.write_text(updated, encoding="utf-8")
    return 0


COURIER_GROUP = commands.Group(name="courier", help="the courier bus: hand a reference to another label", description="", epilog="")
COURIER_COMMANDS = [
    commands.Command(
        "send", "courier", "append a bus entry naming a courier reference",
        (commands.Arg(("ref",)), commands.Arg(("--to",), {"required": True}), commands.Arg(("--note",), {"required": True}), commands.Arg(("--profile",))),
        _courier_send, False, (),
    ),
    commands.Command(
        "inbox", "courier", "list this label's unacknowledged bus entries",
        (commands.Arg(("--label",)), commands.Arg(("--profile",))),
        _courier_inbox, False, (),
    ),
    commands.Command(
        "ack", "courier", "acknowledge one bus entry by id",
        (commands.Arg(("id",)), commands.Arg(("--profile",))),
        _courier_ack, False, (),
    ),
]

PLAN_GROUP = commands.Group(
    name="plan", help="visual plans through the local bridge",
    description="Visual plans through the local bridge.",
    epilog="examples:\n  cox plan serve work/<id> --kind plan\n  cox plan serve work/<id> --check",
)
PLAN_COMMANDS = [
    commands.Command(
        "serve", "plan", "serve a visual plan through the local bridge",
        (commands.Arg(("dir",)), commands.Arg(("--kind",), {"default": "plan"}), commands.Arg(("--check",), {"action": "store_true"}), commands.Arg(("--no-open",), {"action": "store_true"})),
        _plan_serve, False, (),
    ),
]

EPIC_GROUP = commands.Group(
    name="epic", help="watch a detached run",
    description="Watch a detached run.",
    epilog="examples:\n  cox epic watch RUN.pid --log RUN.log\n  cox epic watch RUN.pid --json",
)
EPIC_COMMANDS = [
    commands.Command(
        "watch", "epic", "poll a detached run's pidfile until it exits",
        (
            commands.Arg(("pidfile",)), commands.Arg(("--log",)),
            commands.Arg(("--max-seconds",), {"type": float, "default": 570}), commands.Arg(("--interval",), {"type": float, "default": 20}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _epic_watch, False, (),
    ),
]

ROUTE_GROUP = commands.Group(
    name="route", help="file work for the harness, and see what is queued or running",
    description="File work for the harness, and see what is queued or running.",
    epilog="examples:\n  cox route launch epic --initiative work/<id> --repo PATH\n  cox route status --profile PATH",
)
_CHAIR_PID_ARG = commands.Arg(("--pid",), {"type": int, "help": "the durable pid that owns the loop (default: the parent process)"})
_LAUNCH_SHARED_ARGS = (
    commands.Arg(("--tier-ceiling",), {"choices": ("cheap", "standard", "deep")}),
    commands.Arg(("--effort-ceiling",), {"choices": ("low", "high")}),
    commands.Arg(("--force",), {"action": "store_true", "help": "launch despite a usage stop or a foreign live leader"}),
    commands.Arg(("--no-claim",), {"action": "store_true", "help": "launch without taking an unheld or stale loop"}),
    commands.Arg(("--label",)),
)
ROUTE_COMMANDS = [
    commands.Command(
        "context", "route", "the routing profile's resolved context",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        _route_context, False, (),
    ),
    commands.Command(
        "status", "route", "what is queued or running for this profile",
        (
            commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "every run, not only live and recent ones"}),
        ),
        _route_status, False, (),
    ),
    commands.Command(
        "file", "route", "file a new ticket for the harness",
        (
            commands.Arg(("--profile",)), commands.Arg(("--repo",)), commands.Arg(("--title",)), commands.Arg(("--body",)),
            commands.Arg(("--phase",), {"default": "build"}), commands.Arg(("--intake",), {"action": "store_true"}),
            commands.Arg(("--from-intake",), {"help": "link and file an existing intake file's initiative, then retire it"}),
        ),
        _route_file, False, (),
    ),
    commands.Command(
        "pull", "route", "file intake tickets from a source, once per link",
        (
            commands.Arg(("--profile",)), commands.Arg(("--source",), {"default": "github"}),
            commands.Arg(("--dry-run",), {"action": "store_true"}),
        ),
        _route_pull, False, (),
    ),
    commands.Command(
        "lint", "route", "static ticket lint over a filed initiative, work-shape.md §3",
        (commands.Arg(("initiative_dir",)), commands.Arg(("--repo",), {"default": None})),
        _route_lint, False, (),
    ),
    commands.Command(
        "groups", "route", "print the newest plans/intake-groups/<date>.md file, work-shape.md §5",
        (commands.Arg(("--profile",)),),
        _route_groups, False, (),
    ),
    commands.Command(
        "drift", "route", "items whose store state and file state differ",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        _route_drift, False, (),
    ),
    commands.Command(
        "import", "route", "load the current work item files into the store's work_items table",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        _route_import, False, (),
    ),
    commands.Command(
        "approve", "route", "approve a draft initiative's todo tickets: todo becomes ready",
        (
            commands.Arg(("initiative",)), commands.Arg(("--task",), {"help": "approve this one task only"}),
            commands.Arg(("--by",), {"help": "who approves; default the OS user"}), commands.Arg(("--profile",)),
        ),
        _route_approve, False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "decline", "route", "decline a draft initiative: its todo tickets become dropped",
        (
            commands.Arg(("initiative",)), commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who declines; default the OS user"}), commands.Arg(("--profile",)),
        ),
        _route_decline, False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "chair", "route", "the chair lock for the landing loop (runs/chair.json)", (), None, False, (),
        subcommands=(
            commands.Command(
                "take", "route", "take the chair lock if no live chair holds it",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--steal",), {"action": "store_true"}),
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "how long the takeover lasts before the loop may steal it (default: policy.leader.takeover_hours)"}),
                ),
                _route_chair_take, False, (),
            ),
            commands.Command(
                "extend", "route", "move the end of the chair takeover this session holds",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "hours from now (default: policy.leader.takeover_hours)"}),
                ),
                _route_chair_extend, False, (),
            ),
            commands.Command(
                "beat", "route", "refresh the chair lock's heartbeat",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--run",)),
                ),
                _route_chair_beat, False, (),
            ),
            commands.Command(
                "release", "route", "release the chair lock this session holds",
                (commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG),
                _route_chair_release, False, (),
            ),
            commands.Command(
                "status", "route", "the chair lock's holder and computed state",
                (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
                _route_chair_status, False, (),
            ),
            commands.Command(
                "clear", "route", "remove the chair lock file, refusing a live holder unless --force",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--force",), {"action": "store_true", "help": "clear the lock even if its recorded pid is live"}),
                ),
                _route_chair_clear, False, (),
            ),
            commands.Command(
                "chat", "route", "append to or read the leader chat thread (runs/leader.chat.jsonl)",
                (
                    commands.Arg(("text",), {"nargs": "?"}), commands.Arg(("--profile",)),
                    commands.Arg(("--read",), {"action": "store_true"}),
                    commands.Arg(("--since",)), commands.Arg(("--json",), {"action": "store_true"}),
                    commands.Arg(("--as-leader",), {"action": "store_true", "help": "send as the lock's holder; refuses unless this process is the live holder"}),
                ),
                _route_chair_chat, False, (),
            ),
        ),
        sub_dest="chair_cmd", sub_required=False,
    ),
    commands.Command(
        "sync", "route", "mirror the work store onto the profile's tracker",
        (
            commands.Arg(("--profile",)), commands.Arg(("--item",)), commands.Arg(("--project",)),
            commands.Arg(("--workspace",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
        ),
        _route_sync, False, (),
    ),
    commands.Command(
        "launch", "route", "run one of the harness's graphs directly", (), None, False, (),
        subcommands=(
            commands.Command(
                "epic", "route", "launch the epic graph against a filed initiative",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--fix-attempts",), {"type": int, "default": None}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    commands.Arg(("--on",), {"help": "start the lane on this profile lane_hosts entry instead of locally"}),
                    commands.Arg(("--include-blocked",), {"action": "store_true", "help": "launch despite a ready task behind a blocked item (--force does not)"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                _route_launch, False, (), defaults={"graph": "epic"},
            ),
            commands.Command(
                "decompose", "route", "launch the decompose graph against an idea",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--idea",), {"required": True}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                _route_launch, False, (), defaults={"graph": "decompose"},
            ),
            commands.Command(
                "rescue", "route", "launch the rescue graph against a task",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--task",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                _route_launch, False, (), defaults={"graph": "rescue"},
            ),
            commands.Command(
                "cos", "route", "launch the cos graph",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                _route_launch, False, (), defaults={"graph": "cos"},
            ),
            commands.Command(
                "sweep", "route", "launch the sweep graph against an idea",
                (
                    commands.Arg(("--idea",), {"required": True}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--label",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                ),
                _route_launch_sweep, False, (),
            ),
        ),
        sub_dest="graph", sub_required=True,
    ),
]

def _setup_doctor_row(a: argparse.Namespace) -> int:
    """Indirect through the module global, not a frozen reference: SETUP_COMMANDS
    is built once at import, but test_setup_screen.py monkeypatches
    `cli._setup_doctor` per test and expects the swap honoured."""
    return _setup_doctor(a)


SETUP_GROUP = commands.Group(
    name="setup", help="does this machine's profile actually work",
    description="Does this machine's profile actually work.",
    epilog="examples:\n  cox setup doctor --profile PATH\n  cox setup install --root PATH --team NAME --workspace PATH",
    args=(commands.Arg(("--profile",)),),
)
SETUP_COMMANDS = [
    commands.Command(
        "doctor", "setup", "check this machine's profile against what it needs",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--repo",), {"help": "target repo to check for a .agent/cartridge.yaml overlay (default: cwd)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--host",), {"help": "run the doctor on this lane host from lane_hosts; --json is ignored"}),
        ),
        _setup_doctor_row, False, (),
    ),
    commands.Command(
        "install", "setup", "clone components and write a profile for this machine",
        (
            commands.Arg(("--root",), {"required": True}), commands.Arg(("--team",), {"required": True}), commands.Arg(("--workspace",), {"required": True}),
            commands.Arg(("--provider-profile",)), commands.Arg(("--skills-root",)), commands.Arg(("--assume",), {"default": "a", "choices": ("a", "r")}),
            commands.Arg(("--plugins",), {"action": "store_true"}), commands.Arg(("--hook",), {"action": "store_true"}),
            commands.Arg(("--force-profile",), {"action": "store_true"}), commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--window-ceiling-usd",), {"type": float, "default": None, "dest": "window_ceiling_usd", "help": "write spend: window_ceiling_usd into the profile"}),
            commands.Arg(("--weekly-ceiling-usd",), {"type": float, "default": None, "dest": "weekly_ceiling_usd", "help": "write spend: weekly_ceiling_usd into the profile"}),
        ),
        _setup_install, False, (),
    ),
]


@dataclasses.dataclass(frozen=True)
class _ChairUnwired:
    """A reader or recorder with no edge yet; `_chair_run` refuses to start while any field of either bundle holds one."""

    source: str

    def __call__(self, *_args: object) -> object:
        raise RuntimeError(f"chair run: the {self.source} source is not wired to a reader yet")


def _chair_unwired_sources(deps: chair_run.RunDeps) -> list[str]:
    """Named unwired fields of the facts and exec bundles, in field order."""
    return [
        v.source
        for bundle in (deps.facts_deps, deps.exec_deps)
        for f in dataclasses.fields(bundle)
        if isinstance(v := getattr(bundle, f.name), _ChairUnwired)
    ]


def _chair_once_exit(last_line: str) -> int:
    """1 when the status line has the shape `chair_run.error_line` prints; a test builds one with it, so a format change fails there."""
    return 1 if "| tick error: " in last_line else 0


# The graphs dispatch loop's default (`_DEFAULT_MAX_IN_FLIGHT` in harness/cos.py), used when no cartridge or policy file names a cap.
_CHAIR_MAX_IN_FLIGHT = 3


def _cartridge_cap(cartridges_dir: Path, team: str) -> int | None:
    """Edge. The first `policy.dispatch.max_in_flight` along the team cartridge and its `extends` chain, all read from `cartridges_dir`."""
    pending, seen = [team], set()
    while pending:
        name = pending.pop(0)
        if name in seen:
            continue
        seen.add(name)
        text = _read_text_or_none(cartridges_dir / name / "cartridge.yaml")
        cap = chair_cap.cap_from_cartridge(text)
        if cap is not None:
            return cap
        pending.extend(chair_cap.extends_of(text))
    return None


def _chair_max_in_flight(runs_dir: Path, profile: dict) -> int:
    """Edge. The lane cap: the team cartridge's `policy.dispatch.max_in_flight`, else its `extends` chain's, else `<runs_dir>/policy.pacing.json`'s, else 3."""
    cartridges_dir, team = profile.get("cartridges_dir"), profile.get("team")
    from_cartridge = _cartridge_cap(Path(cartridges_dir).expanduser(), team) if cartridges_dir and isinstance(team, str) and team else None
    if from_cartridge is not None:
        return from_cartridge
    text = _read_text_or_none(runs_dir / "policy.pacing.json")
    try:
        raw = json.loads(text) if text is not None else {}
    except json.JSONDecodeError:
        raw = {}
    value = raw.get("max_in_flight") if isinstance(raw, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else _CHAIR_MAX_IN_FLIGHT


def _chair_stranded_inputs(ws: Path, mode: str) -> tuple[list[dict], list[dict]]:
    """Edge. The inputs of `runs_stranded.stranded`. Item states come from the store under mode "store", as the docket's do."""
    task_records = []
    for path in sorted((ws / "runs").glob("*/tasks/*/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.setdefault("run", path.parent.parent.parent.name)
        record.setdefault("task", path.stem)
        record.setdefault("phase", path.parent.name)
        task_records.append(record)
    initiative_texts = _initiative_texts(ws)
    items = [
        {**item, "repo": route.parse_frontmatter(initiative_texts.get(item["initiative"], ""))[0].get("repo")}
        for item in _stored_work_items(ws, mode)
    ]
    return task_records, items


def _chair_reported_repos_path(runs_dir: Path) -> Path:
    """Beside `chair.lease.json`: the persisted set of repo paths already reported missing."""
    return runs_dir / "chair.reported_repos.json"


def _read_reported_repos(runs_dir: Path) -> set[str]:
    raw = _json_or(_read_text_or_none(_chair_reported_repos_path(runs_dir)), [])
    return set(raw) if isinstance(raw, list) else set()


def _write_reported_repos(runs_dir: Path, repos: list[str]) -> None:
    _chair_reported_repos_path(runs_dir).write_text(json.dumps(sorted(set(repos))), encoding="utf-8")


def _live_by_host(lanes: Sequence[run_store.Lane], local: str) -> dict[str, int]:
    """Live lanes per host name; a lane with no host, or on `local`, counts under the empty name."""
    names = [("" if lane.host in (None, local) else lane.host) for lane in lanes]
    return {name: names.count(name) for name in dict.fromkeys(names)}


def _dispatch_counts(lanes: Sequence[run_store.Lane], local: str, listed: Sequence[str], pidfile_live: int) -> dict[str, int]:
    """`lanes` are the store lanes no local pidfile names. Live pidfile runs and lanes on a host the profile does not list count as local, so the total is what `busy_lanes` counted."""
    by_host = _live_by_host(lanes, local)
    return {"": pidfile_live + sum(n for name, n in by_host.items() if name not in listed), **{name: by_host.get(name, 0) for name in listed}}


def _live_by_host_name(lanes: Sequence[run_store.Lane], local: str) -> dict[str, int]:
    """Live lanes per hosts-table row name. `_live_by_host` files this machine's lanes under "", but `cox host beat` names
    this machine's row `local`, so the "" count is re-keyed to `local`. Every live lane counts: the pidfile filter
    `_dispatch_counts` applies exists only because it adds the pidfile runs back, and a host list adds nothing back."""
    return {(local if name == "" else name): n for name, n in _live_by_host(lanes, local).items()}


def _record_beat_wanted(dry_run: bool, lost: str) -> bool:
    """The record file is beaten only by a live run whose lease renewal came back without a refusal."""
    if dry_run:
        return False
    return lost == ""


def _chair_run_deps(
    runs_dir: Path, profile: dict, session: str, pid: int, host: str, dry_run: bool, echo: Callable[[str], None],
    profile_path: Path, mode: str,
) -> chair_run.RunDeps:
    """Every source has an edge reader; `_ChairUnwired` stays for a source that loses one. A dry run never beats the lease."""
    holder = chair.lease_holder(session, pid, host)
    ws = runs_dir.parent

    def now() -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC)

    def now_text() -> str:
        return now().strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot: list[dict] = []  # edge state: `beat` empties it, so one tick's three docket readers share one read

    def docket() -> dict:
        if not snapshot:
            snapshot.append(chair_read_docket.read_docket(ws, mode, _chair_max_in_flight(runs_dir, profile)))
        return snapshot[0]

    def beat() -> object:
        snapshot.clear()
        lost = "" if dry_run else chair.renew_lease(runs_dir, session, pid, host)
        if _record_beat_wanted(dry_run, lost):
            with chair.locked(runs_dir):
                moved, _ = chair.beat(chair.read(runs_dir), session, pid, host, now())
                if moved is not None:
                    chair.write(runs_dir, moved)
        return lost

    def live_initiatives() -> list[str]:
        return chair_read_live.read_live_initiatives(runs_dir, [row["id"] for row in docket()["initiatives"]], now_text())

    def approved() -> list:
        stranded = chair_read_stranded.read_stranded(*_chair_stranded_inputs(ws, mode))
        facts = chair_read_approved.read_fetch_facts(runs_dir, stranded)
        return chair_read_approved.with_runs(chair_read_approved.read_approved(ws, mode), stranded, facts)

    last_missing_repos: list[str] = []  # edge state: the current tick's `missing_repos()` result, read back by `reported_repos()`

    def missing_repos() -> list[str]:
        last_missing_repos[:] = chair_read_stranded.read_missing_repos(*_chair_stranded_inputs(ws, mode), os.path.isdir)
        return last_missing_repos

    def reported_repos() -> set[str]:
        """Returns the set reported as of the *previous* tick, then persists this tick's `missing_repos()`
        as the new reported set, so a still-missing repo is not reported again and one that reappears
        as present is dropped. A dry run never persists, matching `beat`'s own rule."""
        previous = _read_reported_repos(runs_dir)
        if not dry_run:
            _write_reported_repos(runs_dir, last_missing_repos)
        return previous

    def epoch() -> int:
        lease = chair._read_lease(runs_dir, holder)
        return lease["epoch"] if lease is not None else -1  # no lease matches no action's epoch, so all are fenced

    def has_patch(initiative: str, task: str) -> bool:
        """`read_has_patch` needs a run id: the newest attempt on this task names it. No attempt has no record."""
        runs = [a["run"] for a in chair_read_attempts.read_attempts(ws) if a.get("initiative") == initiative and a.get("task") == task and a.get("run")]
        return bool(runs) and chair_read_patch.read_has_patch(runs_dir, runs[-1], task)

    def repo_for(action: dict) -> str:
        """The action's own repo, else `repo:` from the initiative's `initiative.md`; "" when either is missing."""
        if action.get("repo"):
            return action["repo"]
        initiative = action.get("initiative", "")
        if not initiative:
            return ""
        try:
            text = (ws / "work" / initiative / "initiative.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        return route.parse_frontmatter(text)[0].get("repo") or ""

    profile_text = _read_text_or_none(profile_path) or ""  # the `profile` dict never carries lane_hosts
    parsed_hosts = _profile_lane_hosts(profile_text)
    if isinstance(parsed_hosts, lane_hosts.LaneHostError):
        echo(f"chair run: lane_hosts ignored: {parsed_hosts.message}")
    startup_rows = run_store.hosts(runs_dir)
    startup_hosts = _lane_host_candidates(profile_text, startup_rows)
    unreachable = [] if isinstance(startup_hosts, lane_hosts.LaneHostError) else host_cmd.dispatchable(startup_hosts)[1]
    if unreachable:
        echo(f"chair run: not dispatching to {', '.join(unreachable)}: no workspace_dir; run `cox host beat` on each")
    shadow = _shadowed_note(profile_text, [str(r["name"]) for r in startup_rows])
    if shadow:
        echo(f"chair run: {shadow}")

    def dispatch(row: dict) -> chair_facts.DispatchFacts:
        stamp = now_text()
        pidfile_live, pidfiled = chair_read_docket.local_runs(runs_dir, stamp)
        lanes = run_store.remote_lanes(run_store.live_lanes(runs_dir, stamp), pidfiled)
        host_rows = run_store.hosts(runs_dir)  # per tick: a drain takes effect without a restart
        candidates = _lane_host_candidates(profile_text, host_rows)
        # a host with no workspace_dir is refused by `route launch --on`, so the loop must not plan launches onto it
        host_names = [] if isinstance(candidates, lane_hosts.LaneHostError) else host_cmd.dispatchable(candidates)[0]
        return chair_facts.dispatch_facts(
            row, host_names, _dispatch_counts(lanes, host, host_names, pidfile_live), host_cmd.row_capacities(host_rows)
        )

    weekly_reset = usage_window.parse_weekly_reset(profile.get("weekly_reset"))
    facts_deps = chair_facts.FactsDeps(
        lease=lambda: chair_read_lease.read_lease(runs_dir, now()),
        window=lambda: usage_window.gather(runs_dir, now(), ceiling_usd=profile.get("window_ceiling_usd")),
        weekly=lambda: usage_window.gather_weekly(
            runs_dir, now(), profile.get("weekly_ceiling_usd"),
            store_spend=lambda since: run_store.cost_since(runs_dir, since),
            reset=weekly_reset,
        ),
        policy=lambda: _resolved_pacing_policy(runs_dir),
        docket=docket,
        approved=approved,
        quarantined=lambda: chair_read_quarantined.read_quarantined(ws, mode),
        stranded=lambda: chair_read_stranded.read_stranded(*_chair_stranded_inputs(ws, mode)),
        missing_repos=missing_repos,
        reported_repos=reported_repos,
        run_exited=lambda: chair_read_exits.exits(runs_dir),
        attempts=lambda: chair_read_attempts.read_attempts(ws),
        has_patch=has_patch,
        live_initiatives=live_initiatives,
        intake=lambda: chair_read_intake.read_intake(ws),
        work_store_ready=lambda: chair_read_docket.work_store_ready(docket()),
        sources_configured=lambda: chair_read_intake.read_sources_configured(profile_path),
        session=session, pid=pid, host=host, dispatch=dispatch,
        drafts=lambda: draft_list.count_drafts(draft_list.read_drafts(ws / "work", now_text())),
        remote_unfetched=lambda: chair_read_remote_unfetched.read_remote_unfetched(
            runs_dir, [row["id"] for row in docket()["initiatives"]]
        ),
        lost_runs=lambda: chair_read_lost.read_lost_runs(runs_dir, now_text()),
        history=lambda: chair_read_housekeeping.read_last_housekeeping(runs_dir),
        housekeeping_hours=lambda: (profile.get("chair") or {}).get("housekeeping_hours"),
    )
    exec_deps = chair_exec.edge_deps(
        runs_dir, ws, session, pid,
        run_id=chair_read_run_id.make_run_id(runs_dir), repo_for=repo_for,
        record=chair_read_record.recorder(runs_dir, epoch, now_text, store=store_cli.runner(runs_dir), holder=holder),
        host=host,
    )
    return chair_run.RunDeps(
        facts_deps=facts_deps, exec_deps=exec_deps, report_deps=chair_report.Deps(echo=echo),
        beat=beat, current_epoch=epoch,
        holds=lambda: chair._read_lease(runs_dir, holder) is not None,
        release=lambda: chair.release_lease(runs_dir, session, pid, host), sleep=time.sleep, now=now,
    )


def _chair_run(a: argparse.Namespace) -> int:
    """Refuses before any beat while a fact source or recorder is unwired, so a chair that cannot act never holds the lease."""
    profile, runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    last: collections.deque[str] = collections.deque([""], maxlen=1)

    def echo(line: str) -> None:
        chair_report.echo_line(line)
        last.append(line)

    provider, problem = _lake_provider(a)
    if problem:  # an unreadable provider profile would read as mode "files" and relaunch landed work
        print(f"chair run: refusing to start, no lease taken; lake: {problem}")
        return 2
    deps = _chair_run_deps(
        runs_dir, profile, _holder_label(a), os.getpid(), socket.gethostname(), a.dry_run, echo,
        _profile_path(a), work_state.work_state_mode(provider),
    )
    missing = _chair_unwired_sources(deps)
    if missing:
        print(f"chair run: refusing to start, no lease taken; not wired: {', '.join(missing)}")
        return 2
    chair_run.run(a.once, a.interval, a.dry_run, deps)
    if a.once and deps.holds():  # a one-shot process exits here, so its lease must not outlive it
        deps.release()
    return _chair_once_exit(last[0]) if a.once else 0


def _chair_service_environment_file(a: argparse.Namespace) -> str | None:
    if a.environment_file:
        return os.path.abspath(Path(a.environment_file).expanduser())
    garage = Path("~/.config/agent-tools/garage.env").expanduser()
    return str(garage) if garage.is_file() else None


def _chair_service_install(a: argparse.Namespace, path: Path) -> int:
    """Refuses before any write when systemd would read a value differently from the shell that ran the install."""
    cox = shutil.which("cox")
    if cox is None:
        print("chair service: cox is not on PATH")
        return 2
    profile, _runs_dir, refuse_rc = _leader_runs_dir_or_refuse(a)
    if refuse_rc is not None:
        return refuse_rc
    env_file = _chair_service_environment_file(a)
    # systemd does not expand `~/` in WorkingDirectory=, and the unit does not inherit this shell's
    # $AGENT_TOOLS_PROFILE, so every path is absolute and the profile is pinned in ExecStart.
    values = {
        "cox": os.path.abspath(cox),
        "workspace_dir": os.path.abspath(Path(profile["workspace_dir"]).expanduser()),
        "--label": _holder_label(a),
        "--profile": os.path.abspath(_profile_path(a)),
        **({"--environment-file": env_file} if env_file is not None else {}),
    }
    unsafe = chair_service.unsafe_values(values)
    if unsafe:
        print(f"chair service: systemd would split or expand {', '.join(unsafe)}; use only letters, digits and . _ @ + : , / = -")
        return 2
    exports = chair_service.export_lines(_read_text_or_none(Path(env_file)) or "") if env_file is not None else []
    if exports:
        numbers = ", ".join(str(n) for n in exports)
        print(f"chair service: {env_file} has export lines ({numbers}) that EnvironmentFile= may skip; pass --environment-file with NAME=value lines")
        return 2
    text = chair_service.unit_text(
        values["cox"], values["workspace_dir"], values["--label"], a.interval, env_file, values["--profile"],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(path)
    print("\n".join(chair_service.systemctl_lines()))
    return 0


def _chair_service(a: argparse.Namespace) -> int:
    """Edge for the unit text in `chair_service`. Never runs systemctl; `--install` wins when both flags are given."""
    path = chair_service.unit_path(Path.home())
    if a.install:
        return _chair_service_install(a, path)
    if a.status:
        text = _read_text_or_none(path)
        if text is None:
            print(f"no unit at {path}")
        else:
            print(text, end="")
        return 0
    with contextlib.suppress(SystemExit):
        build_parser().parse_args(["chair", "service", "--help"])
    return 0


CHAIR_GROUP = commands.Group(
    name="chair", help="the chair loop that runs the landing and launch ticks itself",
    description="The chair loop that runs the landing and launch ticks itself.",
    epilog="examples:\n  cox chair run --once --dry-run\n  cox chair run --interval 30\n  cox chair service --install",
)
CHAIR_COMMANDS = [
    commands.Command(
        "run", "chair", "beat, gather, plan, perform and report every tick until interrupted",
        (
            commands.Arg(("--once",), {"action": "store_true", "help": "run one tick, release the lease, exit 1 if the tick errored"}),
            commands.Arg(("--interval",), {"type": float, "default": chair_run.DEFAULT_INTERVAL, "help": "seconds between ticks (default: 60)"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "plan and report each tick without taking the lease or performing any action"}),
            commands.Arg(("--label",), {"help": "the chair's holder label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        _chair_run, False, (),
        defaults={"profile": None},
        description=(
            "Each tick beats the chair lease, gathers facts, plans, performs and reports one status line.\n"
            "It yields to `cox route chair take --steal`: actions planned under a lost lease are fenced.\n"
            "It hard-stops at the profile's weekly_hard_stop_fraction of the weekly ceiling."
        ),
    ),
    commands.Command(
        "service", "chair", "write or show the systemd user unit that keeps `cox chair run` alive",
        (
            commands.Arg(("--install",), {"action": "store_true", "help": "write the unit file and print the systemctl lines to run; runs nothing itself"}),
            commands.Arg(("--status",), {"action": "store_true", "help": "print the unit file, or say there is none"}),
            commands.Arg(("--label",), {"help": "the chair's holder label (default: $COX_SESSION_LABEL, else unlabeled)"}),
            commands.Arg(("--interval",), {"type": int, "default": 60, "help": "seconds between ticks (default: 60)"}),
            commands.Arg(("--environment-file",), {"help": "env file the unit loads (default: ~/.config/agent-tools/garage.env when it exists)"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming workspace_dir, pinned in the unit's ExecStart (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
        ),
        _chair_service, False, (),
        description=(
            "--install writes ~/.config/systemd/user/coxswain-chair.service and prints the systemctl lines.\n"
            "It does not run systemctl."
        ),
    ),
]

ROUTER_GROUP = commands.Group(
    name="router", help="the routing-profile flag and the tier decision it gates",
    description="The routing-profile flag and the tier decision it gates.",
    epilog="examples:\n  cox router select --role build\n  cox router select --role build --json",
)
ROUTER_COMMANDS = [
    commands.Command(
        "select", "router", "the effective tier for a role under the profile's router: off|shadow|on flag",
        (
            commands.Arg(("--role",), {"required": True}),
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile and the router flag (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _router_select, False, (),
    ),
]

STEWARD_GROUP = commands.Group(
    name="steward", help="ceiling-change candidates from stats.db, proposed as intake files",
    description="Ceiling-change candidates from stats.db, proposed as intake files.",
    epilog="examples:\n  cox steward propose\n  cox steward propose --json\n  cox steward draft --json",
)
STEWARD_COMMANDS = [
    commands.Command(
        "propose", "steward", "write each candidate clearing the evidence bar as a new intake file; never edits a provider profile",
        (
            commands.Arg(("--db",), {"default": "workspace/stats/stats.db"}),
            commands.Arg(("--profile",), {"help": "the routing profile naming the provider profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _steward_propose, False, (),
    ),
    commands.Command(
        "draft", "steward", "write each grounded proposal in intake as a draft initiative under work/, list those that exist or could not be grounded",
        (
            commands.Arg(("--profile",), {"help": "the routing profile naming the workspace (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        _steward_draft, False, (),
    ),
]


def _run_local(argv: list[str]) -> tuple[int, str]:
    """Edge: an argv's exit code and stdout; a tool that is missing, hangs or cannot run is code 127."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, done.stdout


def _host_store_write(a: argparse.Namespace, argv: list[str]) -> int:
    """Runs `store_cli <argv>` against the store and prints what it answers; non-zero when it refuses."""
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    code, output = store_cli.runner(runs_dir)(argv)
    print(output.strip() if code == 0 else f"host: store_cli exit {code}: {output.strip()}")
    return 0 if code == 0 else 1


def _host_add(a: argparse.Namespace) -> int:
    """After the write, names the profile lane hosts the table now overrides, since they stop being lane hosts."""
    code = _host_store_write(a, host_cmd.add_argv(a.name, a.ssh, a.capacity, _holder_label(a)))
    if code != 0:
        return code
    _, runs_dir, _ = _leader_runs_dir_or_refuse(a)
    names = [a.name, *(str(r["name"]) for r in run_store.hosts(runs_dir))]
    note = _shadowed_note(_read_text_or_none(_profile_path(a)) or "", names)
    if note:
        print(f"note: {note}")
    return 0


def _host_drain(a: argparse.Namespace) -> int:
    return _host_store_write(a, host_cmd.set_state_argv(a.name, "draining", _holder_label(a)))


def _host_activate(a: argparse.Namespace) -> int:
    return _host_store_write(a, host_cmd.set_state_argv(a.name, "active", _holder_label(a)))


def _host_list(a: argparse.Namespace) -> int:
    _, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    rows = run_store.hosts(runs_dir)
    live_by_host = _live_by_host_name(run_store.live_lanes(runs_dir, _now_iso()), socket.gethostname())
    lines = host_cmd.format_host_list(rows, datetime.datetime.now(datetime.UTC), live_by_host)
    print("\n".join(lines) if rows else "no hosts")
    return 0


def _cartridges_checkout(profile: Mapping) -> str:
    """The checkout that holds `provider_profile`; `cartridges_dir` when the profile names no provider profile."""
    provider, cartridges = profile.get("provider_profile"), profile.get("cartridges_dir")
    if provider:
        return str(Path(provider).expanduser().parent)
    return str(Path(cartridges).expanduser()) if cartridges else ""


def _host_repos(profile: Mapping) -> list[str]:
    """Edge: the harness, cartridges and tools checkouts, the three paths the chair itself runs from."""
    harness = str(Path(profile["harness_dir"]).expanduser()) if profile.get("harness_dir") else ""
    return [p for p in (harness, _cartridges_checkout(profile), str(Path(__file__).resolve().parents[1])) if p]


def _package_version() -> str | None:
    try:
        return importlib.metadata.version("coxswain-tools")
    except importlib.metadata.PackageNotFoundError:
        return None


def _host_beat(a: argparse.Namespace) -> int:
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    harness = str(Path(profile["harness_dir"]).expanduser()) if profile.get("harness_dir") else None
    workspace = str(Path(profile["workspace_dir"]).expanduser()) if profile.get("workspace_dir") else None
    versions = host_cmd.beat_versions(
        _run_local, _package_version(), harness, _cartridges_checkout(profile) or None, workspace, _host_repos(profile),
    )
    return _host_store_write(a, host_cmd.beat_argv(a.name or socket.gethostname(), versions))


def _host_ssh(a: argparse.Namespace, profile: Mapping, runs_dir: Path) -> lane_hosts.LaneHost | None:
    """The named host from the table (any state), else from the profile's `lane_hosts`."""
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.name), None)
    if row is not None:
        return lane_hosts.LaneHost(str(row["name"]), str(row["ssh"]), "")
    parsed = _profile_lane_hosts(_read_text_or_none(_profile_path(a)) or "")
    return None if isinstance(parsed, lane_hosts.LaneHostError) else lane_hosts.find_lane_host(parsed, a.name)


def _host_doctor(a: argparse.Namespace) -> int:
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.name), None)
    print(host_cmd.doctor_line(row, datetime.datetime.now(datetime.UTC)))
    host = _host_ssh(a, profile, runs_dir)
    if host is None:
        print(f"unknown host: {a.name}")
        return 1
    code, rows = remote_doctor.doctor_on_host(host, _run_ssh)
    print(f"doctor on {host.name} ({host.ssh})")
    print("\n".join(rows))
    return code


def _host_sync(a: argparse.Namespace) -> int:
    profile, runs_dir, rc = _leader_runs_dir_or_refuse(a)
    if rc is not None:
        return rc
    host = _host_ssh(a, profile, runs_dir)
    if host is None:
        print(f"unknown host: {a.name}")
        return 1
    row = next((r for r in run_store.hosts(runs_dir) if r["name"] == a.name), None)
    repos = host_cmd.recorded_repos(row)
    if not repos:
        print(f"paths: this machine's, since {a.name} has not beaten with its own; run `cox host beat {a.name}` there")
    code, lines = host_cmd.sync_host(host.ssh, repos or _host_repos(profile), _run_ssh)
    print("\n".join(lines))
    return code


HOST_GROUP = commands.Group(
    name="host", help="the hosts table: lane hosts, their capacity, versions and login",
    description="The hosts table: lane hosts, their capacity, versions and login.",
    epilog="examples:\n  cox host add jarvis --ssh jarvis --capacity 8\n  cox host list\n  cox host beat\n  cox host sync jarvis",
    args=(commands.Arg(("--profile",)),),
)
HOST_COMMANDS = [
    commands.Command(
        "add", "host", "add or update a host in the table",
        (
            commands.Arg(("name",)), commands.Arg(("--ssh",), {"required": True}),
            commands.Arg(("--capacity",), {"type": int, "required": True, "help": "lanes the host may run at once"}),
        ),
        _host_add, False, (),
    ),
    commands.Command("list", "host", "one line per host: state, capacity, beat age, login", (), _host_list, False, ()),
    commands.Command("drain", "host", "stop launching on a host; its live lanes finish", (commands.Arg(("name",)),), _host_drain, False, ()),
    commands.Command("activate", "host", "make a host a lane host again", (commands.Arg(("name",)),), _host_activate, False, ()),
    commands.Command(
        "beat", "host", "record this machine's versions and claude login in the table",
        (commands.Arg(("name",), {"nargs": "?", "help": "default: this machine's hostname"}),), _host_beat, False, (),
    ),
    commands.Command("doctor", "host", "the table's line for a host, then its doctor over ssh", (commands.Arg(("name",)),), _host_doctor, False, ()),
    commands.Command(
        "sync", "host", "git pull --ff-only in the harness, cartridges and tools checkouts on a host",
        (commands.Arg(("name",)),), _host_sync, False, (),
    ),
]

COMMAND_TABLE: list[tuple[commands.Group, list[commands.Command]]] = [
    (RUNS_GROUP, RUNS_COMMANDS),
    (COURIER_GROUP, COURIER_COMMANDS),
    (VERSIONS_GROUP, []),
    (INSTALL_GROUP, []),
    (UPGRADE_GROUP, []),
    (HOME_GROUP, []),
    (STATS_GROUP, STATS_COMMANDS),
    (LAKE_GROUP, LAKE_COMMANDS),
    (USAGE_GROUP, USAGE_COMMANDS),
    (PLAN_GROUP, PLAN_COMMANDS),
    (EPIC_GROUP, EPIC_COMMANDS),
    (ROUTE_GROUP, ROUTE_COMMANDS),
    (CHAIR_GROUP, CHAIR_COMMANDS),
    (HOST_GROUP, HOST_COMMANDS),
    (ROUTER_GROUP, ROUTER_COMMANDS),
    (STEWARD_GROUP, STEWARD_COMMANDS),
    (SETUP_GROUP, SETUP_COMMANDS),
]


def _table_entry(name: str) -> tuple[commands.Group, list[commands.Command]]:
    """`COMMAND_TABLE`'s (Group, rows) pair for `name`, so `build_parser` can
    register each migrated group at its own original position -- interleaved
    among the groups still hand-built -- instead of moving every migrated
    group's registration to wherever the table is folded, which would reorder
    `cox --help`'s top-level listing."""
    return next((group, rows) for group, rows in COMMAND_TABLE if group.name == name)


def _launcher(a: argparse.Namespace, extra_args: list[str]) -> int:
    """Bare `cox` (spec §7): a real Claude Code session with the coxswain
    plugin loaded and the profile's workspace as cwd. Resolves the profile
    the same way `setup doctor` does, via `_profile_path`, off the dedicated
    `launcher_profile` dest. A missing profile is the usual help, exit 2 —
    this launcher needs a real workspace to run in, not a one-liner; an
    unreadable one or a missing `workspace_dir` gets the same one-line reason
    `route`'s own commands print, not swallowed into that help text."""
    profile_path = _profile_path(argparse.Namespace(profile=a.launcher_profile))
    text = _read_text_or_none(profile_path)
    if text is None:
        build_parser().print_help()
        return 2
    try:
        profile = route.parse_profile(text)
    except route.ProfileError as exc:
        print(f"routing: profile unreadable: {exc}")
        return 2
    workspace = profile.get("workspace_dir", "")
    if not workspace:
        print(f"routing: workspace_dir not set in profile {profile_path}")
        return 2
    if shutil.which("claude") is None:
        print("claude not found on PATH")
        return 2
    skills_roots = profile.get("skills_roots") or []
    plugin_root = None if a.no_plugin else _plugin_root(skills_roots)
    argv, warning = _launcher_argv(plugin_root, skills_roots, a.no_plugin, extra_args)
    if warning:
        print(warning)
    cwd = str(Path(workspace).expanduser())
    if a.print_argv:
        print(argv)
        print(cwd)
        return 0
    # steal=True: chair.take only lets `steal` override a stale/crashed record
    # (agent_tools/chair.py `take`), never a live one, so a prior bare-`cox`
    # session's own now-unbeaten lock is retaken rather than refused on every
    # later invocation, while a LIVE foreign holder is still only printed.
    chair_a = argparse.Namespace(
        profile=a.launcher_profile,
        label=f"chair-{datetime.datetime.now(datetime.UTC):%Y-%m-%d}",
        pid=os.getpid(),
        steal=True,
    )
    if _route_chair_take(chair_a) == 0:
        runs_dir = Path(workspace).expanduser() / "runs"
        beater_argv = [
            sys.executable, "-m", "agent_tools.chair", "beat-loop",
            "--label", chair_a.label, "--pid", str(chair_a.pid), "--runs-dir", str(runs_dir),
        ]
        try:
            beater = _spawn(beater_argv)
        except OSError as exc:
            print(f"chair: beater failed to start: {exc}")
        else:
            print(f"chair: beating from pid {beater.pid}")
        # chair_a.label is the label `_route_chair_take` just wrote as the holder above;
        # it is the holder, not a re-read of the file this same call just produced.
        _print_inbox(_read_text_or_none(Path(workspace).expanduser() / "courier.jsonl") or "", chair_a.label, chair_a.label)
    os.chdir(cwd)
    os.execvp(argv[0], argv)
    return 0


_TOP_OPTIONS_WITH_VALUE = ("--profile",)
_TOP_FLAGS = ("--no-plugin", "--print-argv")


def _bare_launcher_split(args: list[str]):
    """Splits a bare-`cox` invocation's `-- EXTRA...` tail off before the real
    parser sees it: the top-level subparsers action is a PARSER-style
    positional and would otherwise try to consume the first extra token as an
    (invalid) subcommand name. Returns (head, tail) only when `args`, up to
    any `--`, holds nothing but the launcher's own top-level options; returns
    None the moment a subcommand token (or anything else) appears, so `main`
    leaves that invocation, `--` and all, to the ordinary parser untouched —
    an existing subcommand's own `--` semantics (e.g. `route file -- -title`)
    are not this function's to change."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[:i], args[i + 1 :]
        if tok in _TOP_OPTIONS_WITH_VALUE:
            i += 1
            if i >= len(args):
                return None
        elif tok not in _TOP_FLAGS:
            return None
        i += 1
    return args, []


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args and not sys.stdout.isatty():
        return _route_status(argparse.Namespace(profile=None, json=False, all=False))
    split = _bare_launcher_split(args)
    head, tail = split if split is not None else (args, [])
    a = build_parser().parse_args(head)
    if not hasattr(a, "fn"):
        return _launcher(a, tail)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
