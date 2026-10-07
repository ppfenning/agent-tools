"""The work-side fields of `IdleStallInputs`: free lanes, waiting work, stubs, blocked-by-needs and last progress."""

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from agent_tools import route, run_store
from agent_tools.chair_facts import empty_decompose_facts
from agent_tools.chair_read_docket import local_runs, read_docket
from agent_tools.chair_read_intake import read_intake
from agent_tools.chair_read_live import read_live_initiatives
from agent_tools.chair_read_quarantined import WorkFiles, initiative_files, read_work_items
from agent_tools.chair_read_stale import read_chair_actions
from agent_tools.chair_types import BlockedReady
from agent_tools.draft_state import _is_draft

Item = Mapping[str, Any]

LAUNCH_KINDS = frozenset({"launch_epic", "launch_decompose"})
LAND_KINDS = frozenset({"land", "land_phase"})
# A task in one of these states is launched, landed or unapproved, so it waits on no need.
_NOT_WAITING = route.TERMINAL | {"approved", "in_progress", "draft"}


class IdleBacklog(TypedDict):
    free_lanes: int
    ready: int
    queued: int
    last_progress_at: str | None
    empty_stubs: list[str]
    blocked_ready: list[BlockedReady]


def free_lanes(lanes: int, live_runs: int) -> int:
    return max(lanes - live_runs, 0)


def docket_ready(docket: Mapping[str, Any] | None) -> int:
    """The tasks the launcher would take: the docket's `ready_tasks`, never a second readiness rule."""
    return sum(len(i["ready_tasks"]) for i in docket["initiatives"]) if docket else 0


def _iso_utc(value: str) -> datetime | None:
    """A timestamp read as UTC when it carries no offset; None when it does not parse."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def last_progress_at(last_launch: str | None, last_land: str | None, node_calls: Sequence[str]) -> str | None:
    """The newest of the three, as `YYYY-MM-DDTHH:MM:SSZ`; a value that does not parse is skipped."""
    parsed = [t for v in (last_launch, last_land, *node_calls) if v and (t := _iso_utc(v)) is not None]
    return max(parsed).strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else None


def stub_candidates(initiative_texts: Mapping[str, str]) -> list[str]:
    """Initiative ids with an `initiative.md`, less drafts: a draft has no tasks by design until approved."""
    return sorted(i for i, text in initiative_texts.items() if not _is_draft(text))


def empty_stubs(
    with_file: Collection[str], items: Sequence[Item], decomposed: Mapping[str, str], live: Collection[str]
) -> list[str]:
    """Initiatives `empty_decompose_facts` reports, plus any with a file, no task items and no live run."""
    counts = {i: sum(1 for item in items if item["initiative"] == i) for i in with_file}
    reported = {e["initiative"] for e in empty_decompose_facts(decomposed, live, counts)}
    stubs = {i for i, n in counts.items() if n == 0 and i not in live}
    return sorted(reported | stubs)


def blocked_ready(items: Sequence[Item]) -> list[BlockedReady]:
    """Waiting tasks whose unmet needs are all approved, unlanded tasks of the same initiative, in any phase."""
    rows: list[BlockedReady] = []
    for item in items:
        own = [i for i in items if i["initiative"] == item["initiative"]]
        landed = {i["id"] for i in own if i["state"] in route.TERMINAL}
        approved = {i["id"] for i in own if i["state"] == "approved"}
        unmet = [n for n in item.get("needs", []) if n not in landed]
        if item["state"] not in _NOT_WAITING and unmet and all(n in approved for n in unmet):
            rows.append({"task": item["id"], "unlanded_needs": unmet})
    return rows


def idle_backlog(plain: Mapping[str, Any]) -> IdleBacklog:
    """`plain` keys: docket, items, queued, last_launch, last_land, node_calls, with_file, decomposed, live."""
    docket = plain["docket"]
    lanes, busy = (int(docket["max_in_flight"]), int(docket["busy_lanes"])) if docket else (0, 0)
    return {
        "free_lanes": free_lanes(lanes, busy),
        "ready": docket_ready(docket),
        "queued": len(plain["queued"]),
        "last_progress_at": last_progress_at(plain["last_launch"], plain["last_land"], plain["node_calls"]),
        "empty_stubs": empty_stubs(plain["with_file"], plain["items"], plain["decomposed"], plain["live"]),
        "blocked_ready": blocked_ready(plain["items"]),
    }


def _newest_action(actions: Sequence[Item], kinds: Collection[str]) -> str | None:
    stamps = [
        t for a in actions if a.get("kind") in kinds and a.get("ts") and (t := _iso_utc(str(a["ts"]))) is not None
    ]
    return max(stamps).isoformat() if stamps else None


def _safe(read: Any, default: Any) -> Any:
    try:
        return read()
    except Exception:  # the edge never raises: an unreadable source reads as empty
        return default


def _initiative_texts(ws: Path, files: WorkFiles | None = None) -> dict[str, str]:
    if files is not None:
        return {p.parent.name: t for p, t in initiative_files(ws, files).items()}
    return {p.parent.name: p.read_text(encoding="utf-8") for p in sorted(ws.glob("work/*/initiative.md"))}


def read_idle_backlog(
    ws: Path, mode: str, max_in_flight: int, now: datetime,
    files: WorkFiles | None = None, store_rows: Sequence[Mapping] | None = None,
    actions: Sequence[Item] | None = None,
) -> IdleBacklog:
    """Edge. Never raises. An unreadable docket gives zero lanes, not every lane free.

    `files`, `store_rows` and `actions` stand in for the work-file, store and chair_actions reads when a tick already holds them.
    `actions` is the windowed `FactsDeps.actions()` rows: a land older than the window no longer counts as progress."""
    runs_dir = ws / "runs"
    now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    work = [item for _, _, item in _safe(lambda: read_work_items(ws, mode, files, store_rows), [])]
    with_file = stub_candidates(_safe(lambda: _initiative_texts(ws, files), {}))
    # A stub has no work items, so its id comes from its file; probing only item initiatives never sees it live.
    probe = sorted({i["initiative"] for i in work} | set(with_file))
    # None is the caller that has not been wired to the shared rows yet; it still reads its own.
    rows = actions if actions is not None else _safe(lambda: read_chair_actions(runs_dir), [])
    # `last_call_at` needs run ids; the runs a local pidfile names are the ones this machine can list.
    run_ids = sorted(_safe(lambda: local_runs(runs_dir, now_text)[1], set()))
    calls = _safe(lambda: list(run_store.last_call_at(runs_dir, run_ids).values()), [])
    return idle_backlog(
        {
            "docket": _safe(lambda: read_docket(ws, mode, max_in_flight, lambda: now, files, store_rows), None),
            "items": work,
            "queued": _safe(lambda: read_intake(ws, initiative_files(ws, files) if files is not None else None), []),
            "last_launch": _newest_action(rows, LAUNCH_KINDS),
            "last_land": _newest_action(rows, LAND_KINDS),
            "node_calls": [str(c) for c in calls],
            "with_file": with_file,
            "decomposed": {},  # no caller wires `FactsDeps.decomposed_intake` yet
            "live": _safe(lambda: read_live_initiatives(runs_dir, probe, now_text), []),
        }
    )
