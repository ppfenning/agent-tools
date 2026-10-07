"""The edge of a chair tick: gather `chair_types.Facts` from injected sources.

The shapers are pure and `gather_facts` calls each source once. The only clock is the `now` argument.
No retry state is stored: `harness_failures` is counted from run history on every call.
"""
import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import Any

from agent_tools import chair_read_docket, chair_smoke, forge, pacing, route
from agent_tools.chair import lease_holder
from agent_tools.chair_decompose_streak import DecomposeRun, DecomposeStreak, at_limit, streak
from agent_tools.chair_idle_stall import IDLE_STALL_MINUTES
from agent_tools.chair_plan import initiative_homes
from agent_tools.chair_plan_land import planned_tasks
from agent_tools.chair_read_docket import docket_from_rows
from agent_tools.chair_read_intake import intake_from_rows
from agent_tools.chair_read_quarantined import quarantined_from_rows
from agent_tools.chair_read_stranded import stranded_from_rows
from agent_tools.chair_read_tuning import current_lanes, tuning_facts
from agent_tools.chair_types import (
    EASTERN,
    ApprovedTask,
    CarryTask,
    DispatchFacts,
    EmptyDecomposeFacts,
    Facts,
    HoldRecord,
    HostCheck,
    IdleStallInputs,
    InitiativeFacts,
    LandedMain,
    LandWait,
    LandWatch,
    LeaseFacts,
    LimitsFacts,
    PhaseBranch,
    PidProbeFact,
    QuarantineFacts,
    ReviewPr,
    RunningInitiative,
    StrandedPhase,
)

HARNESS_CAUSE = "harness"
RESCUE_FAILED_CAUSE = "rescue_failed"
STRANDED_CAUSE = "stranded"
LAUNCHING_VERDICTS = frozenset({"go", "go_degraded"})
LAUNCH_KINDS = frozenset({"launch_epic", "relaunch", "retry", "rescue"})
STEER_STATES = frozenset({"ready", "approved"})

Row = Mapping[str, Any]


@dataclass(frozen=True)
class FactsDeps:
    """One callable per source, so a test passes fakes. Each field names the row keys its source returns.

    lease: the store lease record, keys holder, host, epoch, released, stale.
    docket: the `route context` docket, keys initiatives (id, started, ready_tasks, landed), busy_lanes, max_in_flight.
    approved: approved tasks, keys id, initiative, repo, phase, phase_done, needs, run (empty when no record is stranded).
    quarantined: open quarantined work items, keys initiative, phase, task, from each item's work-store path.
    stranded: `runs_stranded.stranded` rows as they land, keys run, task, phase, branch, remedy. No initiative.
    attempts: every attempt over all runs, oldest first, keys run, phase, task, cause, and initiative when known.
        A work item's frontmatter `attempts` entries carry run and cause, as `stats_chair` reads them; the item's
        path adds phase and task. The store's `attempts` table has no task column (`run_store.attempt_causes`).
    has_patch: (initiative, task id) to whether the task's stored record holds a non-blank build.patch. Called once
        per quarantine entry, and False when no record can be found.
    live_initiatives: initiatives with a live run, meaning the `runs:<initiative>` store lease is held and
        unexpired, or failing that `runs/<run>.pid` names a live pid.
    drafts: the number of initiatives waiting for approval. Optional, and absent means 0.
    missing_repos: sorted, de-duplicated repository path strings, the list `runs_stranded` will produce for
        repositories that no longer exist. Optional, and absent means empty.
    reported_repos: the paths already reported on an earlier tick. Optional, and absent means empty.
        A missing repository is reported once, in the status line, and never becomes a needs_chair fact.
    remote_unfetched: initiative id to run id, for each initiative whose newest run carries a remote
        record and no local run directory or log. Optional, and absent means no initiative counts.
    schema_deaths: initiative id to its newest two run ids, for each initiative whose newest two runs both
        exited on the schema-version refusal. Defaults to a callable returning {}.
    lost_runs: initiative id to run id, for each lane whose host has gone stale (no heartbeat for the
        threshold) with no exit record in the store. Optional, and absent means no initiative counts.
    pid_probe: initiative id to `{alive, last_beat_at}`, for a remote run whose host has a fresh beat and
        whose pid was probed. A stale host, or a probe that failed or timed out, has no entry. Defaults to
        a callable returning {}.
    decomposed_intake: initiative id to its most recent decompose run id, for every initiative whose intake
        status is decomposed. Optional, and absent means no initiative counts.
    item_counts: initiative id to the count of task items the work store holds for it. Optional, and absent
        means every initiative counts as non-empty, so none is reported as empty-decompose.
    newest_run_host: initiative id to its newest run's host, "" meaning the local machine
        (`run_store.newest_run_hosts`). Optional, and absent means no initiative counts.
    run_hosts: run id to the host name that ran it, "" meaning the local machine. Defaults to a callable
        returning {}, so no run counts until the edge wires it. `run_host_facts` drops the local entries, so the `run_hosts` fact holds remote runs only.
    hosts: the same seam as `lost_runs`, `run_store.hosts(runs_dir)`'s raw rows from the store's `hosts`
        table, one per host, keys name, state, versions_json, and whatever else that table carries. Stored
        verbatim under the `login_hosts` fact for the login watch to read. Defaults to a callable returning [].
    history: the newest housekeeping action's ISO timestamp (`chair_read_housekeeping.read_last_housekeeping`).
        Optional, and absent means no history, i.e. housekeeping is due.
    housekeeping_hours: the raw profile value at `chair.housekeeping_hours`, resolved by `resolve_housekeeping_hours`.
        Optional, and absent means 24 hours.
    stale_days: the raw profile value at `chair.stale_days`, resolved by `resolve_stale_days`. Optional, and
        absent means 7 days.
    stale_candidates: `stale_candidates(now) -> list[dict]`, bound in production to
        `chair_read_stale.read_stale_candidates`. Optional, and absent means no candidates.
    stall_candidates: `stall_candidates(now) -> list[dict]`, bound in production to
        `chair_read_stall.read_stall_candidates`. Optional, and absent means no candidates.
    idle_stall: `idle_stall(now) -> IdleStallInputs`, bound in production to the four `chair_read_idle_*` readers
        (hosts, lands, backlog, open item) plus the configured `stall_minutes`. Stored unchanged under the
        `idle_stall` fact. Required, so an unwired detector fails at construction instead of planning nothing.
        `idle_stall_inputs` assembles the readers' outputs and `resolve_idle_stall_minutes` the threshold.
    queue: `run_store.read_queue`'s rows for this tick, read once and fed to `chair_read_docket.docket_from_rows`,
        `chair_read_intake.intake_from_rows`, `chair_read_quarantined.quarantined_from_rows` and
        `stranded_records`-paired `chair_read_stranded.stranded_from_rows` to build ready, intake, quarantined and
        stranded facts from the store. Optional, and an absent callable or an empty read falls the four facts back
        to `docket`, `intake`, `quarantined` and `stranded`: an empty read means the store has no queue table or
        harness yet, not that the board is empty.
    stranded_records: the raw per-run task records `chair_read_stranded.stranded_from_rows` pairs against `queue`'s
        task rows, the shape `_chair_stranded_inputs` reads from each run's `tasks/*/*.json` file. Optional, and
        absent leaves `stranded` on the file reader even when `queue` has rows.
    window_source: "meter" when `window` built this tick's figure from a fresh status-line meter entry, else
        "est" for the estimate. Judged from the same per-tick meter read as `window`, so the label cannot
        disagree with the figure. Optional, and absent means "est".
    weekly_source: the same reading as `window_source`, for `weekly`. Optional, and absent means "est".
    runs_dir: the runs directory `limits_facts` reads `chair.hold.json` from, via `chair_smoke.read_hold`.
        Optional, and absent means "", so no hold is ever read and `limits.smoke_hold` stays None.
    review_prs: the approved tasks awaiting a review PR, each with its forge state, as `forge_review_prs` builds
        them in production. Optional, and absent means an empty list.
    tickets: every work item under `work/<initiative>/<phase>/<task>.md`, keys initiative, id, state, surfaces
        (`read_ticket_items`). Optional, and absent means no initiative has surfaces and `running` is empty.
        The store queue is not bound in the chair, so surfaces are never read from `queue`.
    repos: initiative id to the `repo:` of its `initiative.md` (`read_initiative_repos`). Optional, and absent
        means no initiative has a known repo.
    actions: the `chair_actions` rows, keys kind, ts, and action_json or the action's own keys
        (`chair_read_stale.read_chair_actions`). `steer_streaks` derives from them. Optional, and absent means {}.
    run_commits: one row per approved commit a run holds, oldest first, keys task, initiative, phase, run, host,
        branch (the run's agents branch), commit, run_seq. Optional, and absent means no stranded phase is built.
    phase_state: one row per phase with approved work, keys initiative, phase, tasks (every task id of the phase),
        adds (whether landing the phase would add commits over main). Optional, and absent means no stranded phase.
    branch_counts: one row per initiative with a phase branch, keys initiative, phase, branch, ahead, behind, tip,
        counted against main by the git reader. Optional, and absent means no phase branch.
    landing: the lands in progress on the land worker, keys initiative, phase, repo (`WorkerLands.landing`).
        Optional, and absent means an empty list.
    land_watches: the lands not yet resolved, as `LandWatch` rows. Defaults to a callable returning [].
    handoff_stops: the ready or blocked tickets with an attempt on their current body, as `HandoffStop` rows
        (`chair_read_handoff_stops.read_handoff_stops`). Defaults to a callable returning [].
    read_main_ci: (repo, commit) to (state, output) of main's first CI run for that commit, state pending, green
        or red. Defaults to a callable returning pending.
    land_hold: the smoke hold, `chair_smoke.read_hold`'s result. Defaults to a callable returning None.
    land_outcomes: initiative to its resolved land outcomes, oldest first. Defaults to a callable returning {}.
    """

    lease: Callable[[], Row]
    window: Callable[[], pacing.Window]
    weekly: Callable[[], pacing.Window | None]
    policy: Callable[[], pacing.Policy]
    docket: Callable[[], Row]
    approved: Callable[[], Sequence[Row]]
    quarantined: Callable[[], Sequence[Row]]
    stranded: Callable[[], Sequence[Row]]
    attempts: Callable[[], Sequence[Row]]
    has_patch: Callable[[str, str], bool]
    live_initiatives: Callable[[], Collection[str]]
    intake: Callable[[], Sequence[str]]
    work_store_ready: Callable[[], bool]
    sources_configured: Callable[[], bool]
    session: str
    pid: int
    host: str
    idle_stall: Callable[[datetime], IdleStallInputs]  # now -> the idle-lanes stall inputs, stored unchanged
    dispatch: Callable[[Row], DispatchFacts] | None = None  # docket -> lane facts; absent counts every busy lane as local
    queue: Callable[[], Sequence[Row]] | None = None  # run_store.read_queue rows; absent or empty falls back to docket, intake, quarantined and stranded
    stranded_records: Callable[[], Sequence[Row]] | None = None  # paired with queue's task rows to build stranded; absent leaves stranded on the file reader
    drafts: Callable[[], int] | None = None
    missing_repos: Callable[[], list[str]] | None = None
    reported_repos: Callable[[], set[str]] | None = None
    run_exited: Callable[[], Mapping[str, bool]] | None = None  # initiative to whether its newest run has an exit record; absent means {}
    remote_unfetched: Callable[[], Mapping[str, str]] | None = None
    lost_runs: Callable[[], Mapping[str, str]] | None = None  # initiative to run id, for a lane whose host is stale and whose run has no exit record; absent means {}
    decomposed_intake: Callable[[], Mapping[str, str]] | None = None  # initiative to its newest decompose run id, for a decomposed intake; absent means {}
    item_counts: Callable[[], Mapping[str, int]] | None = None  # initiative to its stored task-item count; absent means every initiative counts as non-empty
    newest_run_host: Callable[[], Mapping[str, str]] | None = None  # initiative to its newest run's host, "" meaning local; absent means {}
    run_hosts: Callable[[], Mapping[str, str]] = lambda: {}  # run id to the host that ran it, "" meaning local; absent means {}
    schema_deaths: Callable[[], Mapping[str, list[str]]] = lambda: {}  # initiative to its newest two run ids when both died on the schema refusal; absent means {}
    history: Callable[[], str | None] | None = None  # chair_read_housekeeping.read_last_housekeeping; absent means no history
    housekeeping_hours: Callable[[], object] | None = None  # raw profile chair.housekeeping_hours; absent means 24 hours
    stale_days: Callable[[], object] | None = None  # raw profile chair.stale_days; absent means 7 days
    stale_candidates: Callable[[datetime], Sequence[Row]] | None = None  # chair_read_stale.read_stale_candidates; absent means no candidates
    stall_candidates: Callable[[datetime], Sequence[Row]] | None = None  # chair_read_stall.read_stall_candidates; absent means no candidates
    hosts: Callable[[], list[dict]] = lambda: []  # run_store.hosts(runs_dir) rows, stored verbatim under login_hosts
    window_source: Callable[[], str] = lambda: "est"  # "meter" when `window` built from a fresh meter entry this tick
    weekly_source: Callable[[], str] = lambda: "est"  # "meter" when `weekly` built from a fresh meter entry this tick
    runs_dir: Callable[[], str] = lambda: ""  # the runs directory `limits_facts` reads chair.hold.json from; absent means no hold is ever read
    review_prs: Callable[[], list[ReviewPr]] = lambda: []  # forge_review_prs bound to the profile's forge; absent means []
    pid_probe: Callable[[], Mapping[str, PidProbeFact]] = lambda: {}  # initiative to its remote pid probe; absent means {}
    tickets: Callable[[], Sequence[Row]] | None = None  # `read_ticket_items` rows; absent means no surfaces and no running initiatives
    repos: Callable[[], Mapping[str, str]] | None = None  # initiative to its initiative.md `repo:`; absent means no repo is known
    actions: Callable[[], Sequence[Row]] | None = None  # chair_actions rows; absent means no steer streaks
    decompose_runs: Callable[[str], Sequence[DecomposeRun]] | None = None  # intake path to its decompose runs, oldest first; absent means no decompose streaks
    run_commits: Callable[[], Sequence[Row]] = lambda: []  # approved commits per run, oldest first; absent means []
    phase_state: Callable[[], Sequence[Row]] = lambda: []  # per phase: its task ids and whether a phase land adds over main; absent means []
    branch_counts: Callable[[], Sequence[Row]] = lambda: []  # per phase branch: ahead, behind, tip against main; absent means []
    landing: Callable[[], Sequence[Row]] | None = None  # WorkerLands.landing; absent means []
    tuning: Callable[[datetime], tuple[Sequence[Row], str | None]] = lambda now: ([], None)  # chair_read_tuning.read_tuning bound to its db, runs dir and current tiers; absent means no rows and no last tune
    lane_bounds: Callable[[], Mapping[str, tuple[int, int]]] = lambda: {}  # host ("" local) to the cartridge's (min, max) lanes; absent means {}
    land_watches: Callable[[], Sequence[LandWatch]] = lambda: []  # unresolved lands; absent means landed_main is []
    read_main_ci: Callable[[str, str], tuple[str, str]] = lambda repo, commit: ("pending", "")  # (repo, commit) to (state, output)
    land_hold: Callable[[], HoldRecord | None] = lambda: None  # chair_smoke.read_hold's result; absent means no hold
    land_outcomes: Callable[[], Mapping[str, list[str]]] = lambda: {}  # initiative to resolved outcomes, oldest first; absent means {}
    handoff_stops: Callable[[], Sequence[Row]] = lambda: []  # chair_read_handoff_stops.read_handoff_stops rows; absent means []


def forge_review_prs(runs_dir: str, forge_name: str, resolve: Callable[[str], Any] = forge.forge_for) -> list[ReviewPr]:
    """Edge. The one place a PR's state is read, through the forge registry. A missing forge gives state unknown."""
    # Imported here because the reader imports `run_initiative` from this module.
    from agent_tools.chair_read_review_prs import read_review_prs

    module = resolve(forge_name)  # None for a missing forge, and `forge.pr_state` answers unknown for None
    return read_review_prs(Path(runs_dir), lambda url: forge.pr_state(module, url))


def run_host_facts(raw: Mapping[str, str]) -> dict[str, str]:
    """Run id to host name for the runs that ran on a remote host; a local run (host "") is absent."""
    return {run: host for run, host in raw.items() if host}


def lease_facts(record: Row, session: str, pid: int, host: str) -> LeaseFacts:
    """`mine` needs this holder on a lease that is neither released nor stale."""
    holder = str(record.get("holder") or "")
    released = bool(record.get("released", False))
    stale = bool(record.get("stale", False))
    return {
        "holder": holder,
        "host": str(record.get("host") or host),
        "epoch": int(record.get("epoch") or 0),
        "mine": holder == lease_holder(session, pid, host) and not released and not stale,
        "released": released,
        "stale": stale,
        "expired": bool(record.get("expired", False)),
        "until": str(record.get("until") or ""),
    }


def weekly_fraction(weekly: pacing.Window | None) -> float:
    """Spent over ceiling. No weekly window, or one with no usable ceiling, is unguarded and reads 0.0, as in pacing.assess."""
    if weekly is None or weekly.ceiling_usd is None or weekly.ceiling_usd <= 0:
        return 0.0
    return weekly.spent_usd / weekly.ceiling_usd


WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def window_start_day(start: datetime) -> str:
    """`Sat 23:00 EDT`: the start in EASTERN, as the status line prints its own time.

    `start` arrives in UTC, so formatting it unconverted shows the wrong hour and, near midnight, the wrong day.
    The day name comes from WEEKDAYS, not `%a`, which follows the host's locale."""
    local = start.astimezone(EASTERN)
    return f"{WEEKDAYS[local.weekday()]} {local:%H:%M %Z}"


def limits_facts(
    assessment: pacing.Assessment, policy: pacing.Policy, weekly: pacing.Window | None, max_in_flight: int,
    window_source: str = "est", weekly_source: str = "est", runs_dir: str = "",
) -> LimitsFacts:
    """`launch_cap` is the whole lane budget on a launching verdict and none on `hold` or `stop`.

    `smoke_hold` comes from `chair_smoke.read_hold(runs_dir)`, so a caller such as `chair_plan._launch_cap`
    or the status line sees a held post-land smoke failure, cause and all, without reading the filesystem
    itself. An empty `runs_dir` (the default) never touches the filesystem and reads as no hold."""
    fraction = weekly_fraction(weekly)
    return {
        "hard_stop": fraction >= policy.weekly_hard_stop_fraction,
        "weekly_fraction": fraction,
        "hard_stop_fraction": policy.weekly_hard_stop_fraction,
        "launch_cap": max_in_flight if assessment.verdict in LAUNCHING_VERDICTS else 0,
        "go_degraded": assessment.verdict == "go_degraded",
        "five_hour_fraction": assessment.spent_fraction,
        "window_start_day": window_start_day(weekly.start) if weekly is not None else None,
        "window_source": window_source,
        "weekly_source": weekly_source,
        "smoke_hold": chair_smoke.read_hold(runs_dir) if runs_dir else None,
    }


Key = tuple[str, str, str]


def run_initiative(run: str) -> str:
    """The initiative a run id belongs to: `x-3` is `x`, as the `runs:<initiative>` store lease is named."""
    return re.sub(r"-\d+$", "", run)


def _key(row: Row) -> Key:
    """(initiative, phase, task). A task id repeats across phases, so phase is part of the key.

    A row with no initiative, such as every `runs_stranded` row, takes it from its run id."""
    initiative = str(row.get("initiative") or run_initiative(str(row.get("run") or "")))
    return initiative, str(row.get("phase") or ""), str(row.get("task") or "")


def harness_failures(attempts: Sequence[Row], key: Key) -> int:
    """The task's harness-cause attempts over all runs, on the task's current body. A retry that fails again reads
    one higher. A row with no `on_current_body` counts, as it does today."""
    return sum(
        1
        for a in attempts
        if _key(a) == key and a.get("cause") == HARNESS_CAUSE and a.get("on_current_body", True)
    )


def rescue_failed(attempts: Sequence[Row], key: Key) -> bool:
    """Whether the task has a rescue_failed attempt, over the same attempts and key `harness_failures` counts."""
    return any(_key(a) == key and a.get("cause") == RESCUE_FAILED_CAUSE for a in attempts)


def newest_cause(attempts: Sequence[Row], key: Key) -> str:
    """The cause on the task's newest attempt, from the list `harness_failures` counts. Empty with no attempt."""
    return next((str(a.get("cause") or "") for a in reversed(attempts) if _key(a) == key), "")


def _quarantine(
    key: Key, cause: str, attempts: Sequence[Row], has_patch: Callable[[str, str], bool]
) -> QuarantineFacts:
    return {
        "task_id": key[2],
        "initiative": key[0],
        "cause": cause,
        "harness_failures": harness_failures(attempts, key),
        "has_patch": has_patch(key[0], key[2]),
        "rescue_failed": rescue_failed(attempts, key),
    }


def quarantine_facts(
    quarantined: Sequence[Row],
    stranded: Sequence[Row],
    attempts: Sequence[Row],
    live: Collection[str],
    has_patch: Callable[[str, str], bool],
    landing: Sequence[ApprovedTask] = (),
) -> list[QuarantineFacts]:
    """One entry per open quarantine with its newest cause, then the stranded rows those did not name.

    `landing` is what `chair_plan_land.planned_tasks` plans this tick. A stranded row for one of those tasks
    that carries a run is left out, since its land handles it; a land that then fails raises its own needs_chair.

    An initiative with a live run contributes nothing: a retry in flight is never planned again."""
    opened = list(dict.fromkeys(_key(q) for q in quarantined))
    landed_now = {(t["initiative"], t["phase"], t["id"]) for t in landing if t["run"]}
    # A stranded task that is also an open quarantine keeps the quarantine's newest cause, so it is not sent to the chair as stranded.
    stuck = [k for k in dict.fromkeys(_key(s) for s in stranded) if k not in opened and k not in landed_now]
    return [
        *(_quarantine(k, newest_cause(attempts, k), attempts, has_patch) for k in opened if k[0] not in live),
        *(_quarantine(k, STRANDED_CAUSE, attempts, has_patch) for k in stuck if k[0] not in live),
    ]


def _surfaces_of(item: Row) -> list[str]:
    raw = item.get("surfaces")
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, list) else []


def ticket_surfaces(items: Sequence[Row], ready_ids: Mapping[str, Collection[str]]) -> dict[str, list[str]]:
    """Initiative to the sorted union of the surfaces of its ready tasks; a task with no surfaces adds nothing."""
    return {
        initiative: sorted({s for i in items if i["initiative"] == initiative and i["id"] in ids for s in _surfaces_of(i)})
        for initiative, ids in ready_ids.items()
    }


def running_initiatives(live: Collection[str], items: Sequence[Row], repos: Mapping[str, str]) -> list[RunningInitiative]:
    """One entry per live initiative, by id; surfaces are the sorted union over its ready or approved tasks."""
    return [
        {
            "id": initiative,
            "repo": repos.get(initiative, ""),
            "surfaces": sorted(
                {s for i in items if i["initiative"] == initiative and i.get("state") in STEER_STATES for s in _surfaces_of(i)}
            ),
        }
        for initiative in sorted(live)
    ]


def _action_doc(row: Row) -> Row:
    raw = row.get("action_json")
    doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return {**row, **(doc if isinstance(doc, dict) else {})}


def _streak_step(streaks: Mapping[str, int], doc: Row) -> dict[str, int]:
    initiative = doc.get("initiative") or doc.get("target")
    if doc.get("kind") == "steer_clear" and initiative and doc.get("other"):
        key = f"{initiative}|{doc['other']}"
        return {**streaks, key: streaks.get(key, 0) + 1}
    if doc.get("kind") in LAUNCH_KINDS and initiative:
        return {k: n for k, n in streaks.items() if k.partition("|")[0] != initiative}
    return dict(streaks)


def steer_streaks_from_actions(actions: Sequence[Row]) -> dict[str, int]:
    """`<initiative>|<other>` to its steer_clear count since that initiative's last launch; no record, no key."""
    ordered = sorted(actions, key=lambda r: str(r.get("ts") or ""))
    return reduce(_streak_step, map(_action_doc, ordered), {})


def decompose_stalled_keys(actions: Sequence[Row]) -> list[str]:
    """`<initiative>|<run>` of each decompose_stalled needs_chair row, in row order."""
    docs = [_action_doc(row) for row in actions]
    return [
        f"{d['initiative']}|{d['run']}"
        for d in docs
        if d.get("kind") == "needs_chair" and d.get("cause") == "decompose_stalled" and d.get("initiative") and d.get("run")
    ]


def decompose_streak_facts(
    paths: Sequence[str], runs_for: Callable[[str], Sequence[DecomposeRun]]
) -> dict[str, DecomposeStreak]:
    """Intake path to its streak, for only the paths whose trailing empty runs reach the limit."""
    streaks = {path: streak(runs_for(path)) for path in paths}
    return {path: s for path, s in streaks.items() if at_limit(s)}


def read_ticket_items(ws: Path, mode: str) -> list[dict]:
    """Edge. Every ticket under `ws/work`, parsed as the docket reader parses it, its state from the store in "store" mode."""
    return chair_read_docket._work_items(ws, mode)


def read_initiative_repos(ws: Path) -> dict[str, str]:
    """Edge. Initiative id to the `repo:` in `work/<initiative>/initiative.md`; an unreadable file or no `repo:` is left out."""
    texts = {p.parent.name: chair_read_docket._text(p) for p in sorted((ws / "work").glob("*/initiative.md"))}
    fields = {name: route.parse_frontmatter(text)[0] for name, text in texts.items() if text is not None}
    return {name: f["repo"] for name, f in fields.items() if isinstance(f.get("repo"), str) and f["repo"]}


def initiative_facts(
    docket: Row,
    live: Collection[str],
    surfaces: Mapping[str, list[str]] | None = None,
    repos: Mapping[str, str] | None = None,
) -> list[InitiativeFacts]:
    """The docket's initiatives with no live run, so a live run is never relaunched.

    `repo` and `ready_surfaces` ride along only when their source was read."""
    return [
        {
            "id": i["id"],
            "started": bool(i["started"]),
            "ready_tasks": [
                {"id": t["id"], "needs": list(t["needs"]), "requires": list(t.get("requires", []))}
                for t in i["ready_tasks"]
            ],
            "waiting_tasks": [
                {"id": t["id"], "needs": list(t["needs"]), "requires": list(t.get("requires", []))}
                for t in i.get("waiting_tasks", [])
            ],
            "landed": set(i["landed"]),
            **({"ready_surfaces": list(surfaces.get(i["id"], []))} if surfaces is not None else {}),
            **({"repo": repos[i["id"]]} if repos is not None and i["id"] in repos else {}),
        }
        for i in docket["initiatives"]
        if i["id"] not in live
    ]


def empty_decompose_facts(
    decomposed: Mapping[str, str], live: Collection[str], item_counts: Mapping[str, int]
) -> list[EmptyDecomposeFacts]:
    """One entry per decomposed intake whose decompose run has ended and left zero stored task items.

    `decomposed` maps an initiative id to its newest decompose run id, for every initiative whose intake
    status is decomposed. An initiative in `live` still has that run in flight, so it is left out even
    though its intake is decomposed. `item_counts` defaults a missing initiative to 1, non-empty, so an
    initiative this tick knows nothing about is never reported empty by omission."""
    return [
        {"initiative": initiative, "run": run}
        for initiative, run in decomposed.items()
        if initiative not in live and item_counts.get(initiative, 1) == 0
    ]


def dispatch_facts(
    docket: Row,
    lane_hosts: Sequence[str],
    live_by_host: Mapping[str, int],
    capacity: Mapping[str, int] | None = None,
    weight: Mapping[str, int] | None = None,
    capabilities: Mapping[str, Sequence[str]] | None = None,
    local: str = "",
    local_lanes: str = "any",
) -> DispatchFacts:
    """live_by_host maps a host name to its live lanes; the local machine is under the empty name. A host in `capacity`
    carries its own cap. `weight` and `capabilities` come from the same hosts table row; a name missing from either
    mapping (an older store, or a profile-only host with no row at all) defaults to weight 1 and no capabilities.
    `local` names the machine running this loop: an entry in `lane_hosts` equal to it is never added to the
    returned `hosts`, since the loop's own machine is never a remote lane-host candidate for itself.
    `local_lanes` is carried only when it is not "any", so the default facts stay as they were."""
    caps = capacity or {}
    weights = weight or {}
    abilities = capabilities or {}
    return {
        "max_in_flight": int(docket["max_in_flight"]),
        "live_runs": live_by_host.get("", 0),
        "hosts": [
            {
                "name": name,
                "live_runs": live_by_host.get(name, 0),
                **({"capacity": caps[name]} if name in caps else {}),
                "weight": weights.get(name, 1),
                "capabilities": list(abilities.get(name, [])),
            }
            for name in lane_hosts
            if name != local
        ],
        **({"local_lanes": local_lanes} if local_lanes != "any" else {}),
    }


def approved_facts(rows: Sequence[Row]) -> list[ApprovedTask]:
    return [
        {
            "id": r["id"],
            "initiative": r["initiative"],
            "repo": r["repo"],
            "phase": str(r["phase"]),
            "phase_done": bool(r["phase_done"]),
            "needs": list(r["needs"]),
            "run": str(r["run"]),
            "needs_fetch": bool(r["needs_fetch"]),
        }
        for r in rows
    ]


def phase_branch_facts(rows: Sequence[Row]) -> list[PhaseBranch]:
    """One PhaseBranch per row, in row order; ahead and behind are counts against main."""
    return [
        {
            "initiative": r["initiative"],
            "phase": str(r["phase"]),
            "branch": r["branch"],
            "ahead": int(r["ahead"]),
            "behind": int(r["behind"]),
            "tip": r["tip"],
        }
        for r in rows
    ]


def stranded_facts(
    approved: Sequence[ApprovedTask], commits: Sequence[Row], phases: Sequence[Row]
) -> list[StrandedPhase]:
    """A phase with approved work that adds nothing over main or whose approvals span several runs.

    `commits` is oldest first, so the last row for a task in a run is its newest commit. Carry rows sort by task
    then run_seq; `pending` is the phase's task ids with no approved row, in the phase's order."""
    out: list[StrandedPhase] = []
    for ph in sorted(phases, key=lambda p: (p["initiative"], str(p["phase"]))):
        init, phase = ph["initiative"], str(ph["phase"])
        mine = {a["id"]: a for a in approved if a["initiative"] == init and a["phase"] == phase}
        newest = {
            (c["task"], c["run"]): c
            for c in commits
            if c["initiative"] == init and str(c["phase"]) == phase and c["task"] in mine
        }
        rows: list[CarryTask] = sorted(
            (
                {
                    "task": c["task"],
                    "run": c["run"],
                    "host": c["host"],
                    "branch": c["branch"],
                    "commit": c["commit"],
                    "needs": list(mine[c["task"]]["needs"]),
                    "run_seq": int(c["run_seq"]),
                }
                for c in newest.values()
            ),
            key=lambda r: (r["task"], r["run_seq"]),
        )
        if rows and (not ph["adds"] or len({r["run"] for r in rows}) > 1):
            out.append(
                {
                    "initiative": init,
                    "phase": phase,
                    "phase_branch": f"epic/{init}/{phase}",
                    "approved": rows,
                    "pending": [t for t in ph["tasks"] if t not in mine],
                }
            )
    return out


def new_missing_repos(missing: Collection[str], reported: Collection[str]) -> list[str]:
    """The paths in `missing` not in `reported`, de-duplicated and sorted."""
    return sorted({p for p in missing if p not in reported})


def intake_paths_from_rows(rows: Sequence[Row]) -> list[str]:
    """Queued intake rows as `<task_id>.md`, in row order: the same relative-path shape `read_intake` returns."""
    return [f"{row['task_id']}.md" for row in intake_from_rows(rows)]


DEFAULT_HOUSEKEEPING_HOURS = 24.0
DEFAULT_STALE_DAYS = 7  # chair.stale_days profile default


def resolve_housekeeping_hours(value: object) -> float:
    """The profile's raw `chair.housekeeping_hours`, or 24 when it is missing, non-numeric, or not positive."""
    try:
        hours = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_HOUSEKEEPING_HOURS
    return hours if hours > 0 else DEFAULT_HOUSEKEEPING_HOURS


def resolve_stale_days(value: object) -> int:
    """The profile's raw `chair.stale_days`, or 7 when it is missing or non-numeric."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_STALE_DAYS


def resolve_idle_stall_minutes(value: object) -> int:
    """The profile's raw `chair.idle_stall_minutes`, or 15 when it is missing, non-numeric, or not positive."""
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return IDLE_STALL_MINUTES
    return minutes if minutes > 0 else IDLE_STALL_MINUTES


def idle_stall_inputs(
    backlog: Row,
    hosts: Sequence[HostCheck],
    lands_waiting: Sequence[LandWait],
    open_item: tuple[str | None, str | None],
    stall_minutes: int,
) -> IdleStallInputs:
    """The four idle readers' outputs as one input. `open_item` is `read_idle_open`'s (signature, diagnosis)."""
    return {
        "free_lanes": int(backlog["free_lanes"]),
        "ready": int(backlog["ready"]),
        "queued": int(backlog["queued"]),
        "last_progress_at": backlog["last_progress_at"],
        "stall_minutes": stall_minutes,
        "hosts": list(hosts),
        "empty_stubs": list(backlog["empty_stubs"]),
        "lands_waiting": list(lands_waiting),
        "blocked_ready": list(backlog["blocked_ready"]),
        "open_signature": open_item[0],
        "open_diagnosis": open_item[1],
    }


def _smoke_of(watch: LandWatch, hold: HoldRecord | None) -> tuple[str, str]:
    """Smoke is failed when the hold names this commit, else ok; pending is never produced because no input says a smoke has not run."""
    if hold is not None and hold["land"]["commit"] == watch["commit"]:
        return "failed", hold["tail"]
    return "ok", ""


def landed_main_facts(
    watches: Sequence[LandWatch],
    read_main_ci: Callable[[str, str], tuple[str, str]],
    hold: HoldRecord | None,
) -> list[LandedMain]:
    """One LandedMain per watch, carrying main's first CI state and output for its commit and the smoke verdict."""

    def build(watch: LandWatch) -> LandedMain:
        ci, ci_output = read_main_ci(watch["repo"], watch["commit"])
        smoke, smoke_output = _smoke_of(watch, hold)
        return {**watch, "ci": ci, "ci_output": ci_output, "smoke": smoke, "smoke_output": smoke_output}  # type: ignore[typeddict-item]

    return [build(w) for w in watches]


def gather_facts(deps: FactsDeps, now: datetime) -> Facts:
    policy = deps.policy()
    weekly = deps.weekly()
    assessment = pacing.assess(deps.window(), policy, now, weekly)
    docket = deps.docket()
    # Read once: `rows` drives ready, intake, quarantined and (with stranded_records) stranded below. An empty
    # read means the store has no queue table or harness yet, not that the board is empty, so each fact falls
    # back to its file reader.
    rows = list(deps.queue()) if deps.queue is not None else []
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ready = docket_from_rows(rows, now_iso) if rows else docket["initiatives"]
    dispatch = deps.dispatch(docket) if deps.dispatch else dispatch_facts(docket, [], {"": int(docket["busy_lanes"])})
    live = set(deps.live_initiatives())
    approved = approved_facts(deps.approved())
    items = list(deps.tickets()) if deps.tickets is not None else []
    repos = dict(deps.repos()) if deps.repos is not None else {}
    ready_ids = {i["id"]: {t["id"] for t in i["ready_tasks"]} for i in ready}
    initiatives = initiative_facts(
        {"initiatives": ready}, live,
        ticket_surfaces(items, ready_ids) if deps.tickets is not None else None,
        repos if deps.repos is not None else None,
    )
    newest_run_host = dict(deps.newest_run_host()) if deps.newest_run_host is not None else {}
    # An approved row is, by construction, an approved task not yet landed; `chair_plan_prune.phases_to_carry`
    # draws its carried-partial-phase set from these same rows, so their initiatives cover both cases home tracks.
    unfinished = {r["initiative"] for r in approved}
    quarantined = quarantined_from_rows(rows) if rows else list(deps.quarantined())
    stranded_records = deps.stranded_records() if deps.stranded_records is not None else None
    stranded = stranded_from_rows(rows, list(stranded_records)) if rows and stranded_records is not None else list(deps.stranded())
    intake = intake_paths_from_rows(rows) if rows else list(deps.intake())
    return {
        "lease": lease_facts(deps.lease(), deps.session, deps.pid, deps.host),
        "limits": limits_facts(
            assessment, policy, weekly, dispatch["max_in_flight"], deps.window_source(), deps.weekly_source(),
            deps.runs_dir(),
        ),
        "dispatch": dispatch,
        "approved": approved,
        "home": initiative_homes(newest_run_host, unfinished, deps.host),
        "initiatives": initiatives,
        "quarantines": quarantine_facts(
            quarantined, stranded, deps.attempts(), live, deps.has_patch, planned_tasks(approved, initiatives)
        ),
        "intake": intake,
        "work_store_ready": deps.work_store_ready(),
        "sources_configured": deps.sources_configured(),
        "drafts": deps.drafts() if deps.drafts is not None else 0,
        "missing_repos": new_missing_repos(
            deps.missing_repos() if deps.missing_repos is not None else [],
            deps.reported_repos() if deps.reported_repos is not None else set(),
        ),
        "run_exited": dict(deps.run_exited()) if deps.run_exited is not None else {},
        "remote_unfetched": dict(deps.remote_unfetched()) if deps.remote_unfetched is not None else {},
        "schema_deaths": {i: list(r) for i, r in deps.schema_deaths().items()},
        "lost_runs": dict(deps.lost_runs()) if deps.lost_runs is not None else {},
        "pid_probe": dict(deps.pid_probe()),
        "empty_decompose": empty_decompose_facts(
            dict(deps.decomposed_intake()) if deps.decomposed_intake is not None else {},
            live,
            dict(deps.item_counts()) if deps.item_counts is not None else {},
        ),
        "decompose_streaks": decompose_streak_facts(intake, deps.decompose_runs) if deps.decompose_runs is not None else {},
        "decompose_stalled_reported": decompose_stalled_keys(list(deps.actions())) if deps.actions is not None else [],
        "newest_run_host": newest_run_host,
        "run_hosts": run_host_facts(deps.run_hosts()),
        "login_hosts": deps.hosts(),
        "last_housekeeping_at": deps.history() if deps.history is not None else None,
        "housekeeping_hours": resolve_housekeeping_hours(deps.housekeeping_hours())
        if deps.housekeeping_hours is not None
        else DEFAULT_HOUSEKEEPING_HOURS,
        "stale_candidates": list(deps.stale_candidates(now)) if deps.stale_candidates is not None else [],
        "stall_candidates": list(deps.stall_candidates(now)) if deps.stall_candidates is not None else [],
        "stale_days": resolve_stale_days(deps.stale_days()) if deps.stale_days is not None else DEFAULT_STALE_DAYS,
        "review_prs": list(deps.review_prs()),
        "running": running_initiatives(live, items, repos) if deps.tickets is not None else [],
        "steer_streaks": steer_streaks_from_actions(list(deps.actions())) if deps.actions is not None else {},
        "stranded": stranded_facts(approved, list(deps.run_commits()), list(deps.phase_state())),
        "phase_branches": phase_branch_facts(list(deps.branch_counts())),
        "landing": list(deps.landing()) if deps.landing is not None else [],
        "idle_stall": deps.idle_stall(now),
        "tuning": tuning_facts(
            *deps.tuning(now),
            weekly_fraction(weekly),
            pacing._elapsed_fraction(weekly, now) if weekly is not None else 0.0,
            current_lanes(dispatch), dict(deps.lane_bounds()),
        ),
        "landed_main": landed_main_facts(list(deps.land_watches()), deps.read_main_ci, deps.land_hold()),
        "land_outcomes": dict(deps.land_outcomes()),
        "handoff_stops": list(deps.handoff_stops()),
    }
