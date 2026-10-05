"""The contract every chair-loop planner reads: the facts a tick takes and the actions it returns.

Pure shapes and two pure helpers. No I/O and no harness or store imports. The edge gathers
the facts and executes the actions; the planners in between are pure. `stale_candidates` is
filled in by the facts edge and consumed by the stale planner; `stall_candidates` likewise,
consumed by the stall planner.
"""
from datetime import datetime
from typing import Literal, NotRequired, Protocol, TypedDict
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")  # every time the chair prints for an operator is shown in this zone
REVIEW_CLOSED_CAUSE = "review_closed"  # the needs_chair cause for a task whose review PR was closed without merging


class LeaseFacts(TypedDict):
    holder: str
    host: str
    epoch: int
    mine: bool
    released: bool
    stale: bool
    expired: bool  # another holder's takeover window has passed
    until: str  # that window's end, ISO; empty when the holder set none


class SmokeCommandResult(TypedDict):
    """One post-land smoke command's outcome. ok is True when the process exited 0 and 'Traceback'
    does not appear in its combined output; a 120-second timeout counts as not ok."""

    command: list[str]
    ok: bool
    tail: str  # the last lines of the command's combined output


class LandTrigger(TypedDict):
    """A land that qualifies for a post-land smoke: the repo, PR number, and the new origin/main commit."""

    repo: str
    pr: int
    commit: str


class HoldRecord(TypedDict):
    """Recorded when a post-land smoke fails; cause is always 'smoke_failed'."""

    land: LandTrigger
    failing_command: list[str]  # the argv that failed
    tail: str  # the failing command's last 20 lines
    cause: str


class LimitsFacts(TypedDict):
    """Filled in by the edge from a pacing.assess result; nothing here is computed. launch_cap is the verdict tier ceiling."""

    hard_stop: bool
    weekly_fraction: float
    hard_stop_fraction: float
    launch_cap: int
    go_degraded: bool
    five_hour_fraction: float | None
    window_start_day: str | None  # the weekly window's start in EASTERN, as `Sun 04:00 EDT`; None when there is no weekly window
    window_source: str  # "meter" when the five-hour figure came from a fresh status-line meter entry, else "est"
    weekly_source: str  # the same reading as window_source, for the weekly figure
    smoke_hold: HoldRecord | None  # a held post-land smoke failure, read without importing chair_smoke.py


class HostLanes(TypedDict):
    name: str
    live_runs: int
    capacity: NotRequired[int]
    weight: NotRequired[int]  # a host's share of new placements; absent means 1
    capabilities: NotRequired[list[str]]  # what the host can run, for example ["go"]; absent means none
    # login_ok and login_checked_at mirror the host row's own login state, read from the store's
    # `hosts` table; on the real row they live inside that row's `versions_json`, never as new
    # `hosts` columns, since that table's own columns belong to a different repository.
    login_ok: NotRequired[bool | None]  # None when never checked
    login_checked_at: NotRequired[str]  # ISO UTC of the last check_login attempt; absent means never


class DispatchFacts(TypedDict):
    """live_runs counts the local machine's live lanes; hosts holds each lane host in order, a capacity replacing max_in_flight for that host.
    local_lanes "decompose" reserves the local lanes for intake decomposes; absent means "any"."""

    max_in_flight: int
    live_runs: int
    hosts: list[HostLanes]
    local_lanes: NotRequired[str]


class ApprovedTask(TypedDict):
    id: str
    initiative: str
    repo: str
    phase: str
    phase_done: bool
    needs: list[str]
    run: str  # the newest run whose task record is approved and unlanded; empty when none is
    needs_fetch: bool  # the run is remote and not yet fetched; False without a run


class ReviewPr(TypedDict):
    """An approved task whose review PR the forge is asked about; state is "unknown" when the forge said nothing."""

    initiative: str
    phase: str
    task_id: str
    repo: str
    url: str
    state: Literal["open", "merged", "closed", "unknown"]
    merged_at: str | None


class CarryTask(TypedDict):
    """One approved task commit to cherry-pick; run_seq is higher for a newer run."""

    task: str
    run: str
    host: str
    branch: str  # the run's agents branch
    commit: str  # the newest commit recorded for that task in that run
    needs: list[str]  # task ids it depends on, for ordering
    run_seq: int


class StrandedPhase(TypedDict):
    """A phase with approved work that has not landed."""

    initiative: str
    phase: str
    phase_branch: str
    # One row per approved-but-not-landed task per run holding an approved commit, so a task approved in two runs appears twice.
    approved: list[CarryTask]
    pending: list[str]  # task ids of the phase not yet approved


class PhaseBranch(TypedDict):
    initiative: str
    phase: str
    branch: str
    ahead: int  # commits on the branch that main lacks
    behind: int  # commits on main that the branch lacks
    tip: str


class ReadyTask(TypedDict):
    id: str
    needs: list[str]
    requires: list[str]  # capabilities the ticket's own `requires:` frontmatter field names; empty when it names none


class RunningInitiative(TypedDict):
    """An initiative with a live lane on any machine; surfaces is the union of its ready or approved tasks' surfaces."""

    id: str
    repo: str
    surfaces: list[str]


class InitiativeFacts(TypedDict):
    id: str
    started: bool
    ready_tasks: list[ReadyTask]
    waiting_tasks: NotRequired[list[ReadyTask]]  # state ready, held back by a need not in `landed`
    landed: set[str]
    repo: NotRequired[str]  # the repository the initiative launches into; absent means unknown
    ready_surfaces: NotRequired[list[str]]  # the union of the surfaces of its ready tasks; absent means none


class QuarantineFacts(TypedDict):
    task_id: str
    initiative: str
    cause: str
    harness_failures: int  # counted from run history by the edge
    has_patch: bool  # the task record in the store holds a non-blank build.patch
    rescue_failed: bool  # an earlier rescue_failed attempt exists for this task on the current ticket version


class EmptyDecomposeFacts(TypedDict):
    """A decomposed intake whose newest decompose run has ended and left the initiative with no stored task items."""

    initiative: str
    run: str  # the initiative's most recent decompose run id


class PidProbeFact(TypedDict):
    """One remote run's pid probe against a host with a fresh beat."""

    alive: bool
    last_beat_at: str  # ISO-8601 of the host's newest beat


class StaleCandidate(TypedDict):
    """One task the edge has gathered evidence for; matches chair_stale.stale_reason's parameters exactly."""

    initiative: str
    task_id: str
    state: str
    last_file_change: str | None  # ISO timestamp of the newest file touch under the task, or None
    last_run: str | None  # ISO timestamp of the newest run for the task, or None
    last_chair_action: str | None  # ISO timestamp of the newest chair action recorded for the task, or None
    quarantine_non_harness_count: int  # count of non-harness quarantine causes recorded for the task


class LastCall(TypedDict):
    role: str
    task: str
    ts: str


class StallCandidate(TypedDict):
    """One live run, local or remote; matches chair_stall's idle_minutes/started_minutes parameters."""

    run: str
    initiative: str
    local: bool
    started_at: str
    last_call: LastCall | None
    usr1_sent: bool  # a stalled_usr1 action was already recorded for this run


class Facts(TypedDict):
    lease: LeaseFacts
    limits: LimitsFacts
    stale_days: int  # resolved chair.stale_days profile value, default 7; filled in by the edge
    dispatch: DispatchFacts
    approved: list[ApprovedTask]
    initiatives: list[InitiativeFacts]
    quarantines: list[QuarantineFacts]
    stale_candidates: list[StaleCandidate]  # gathered by the facts edge, consumed by the stale planner
    stall_candidates: list[StallCandidate]  # one entry per currently live run, local and remote; a later task fills it
    intake: list[str]  # oldest first
    work_store_ready: bool
    sources_configured: bool
    drafts: int  # initiatives waiting for approval
    # An initiative's newest run is remote and unfetched: a `<run>.remote.json` beside the runs
    # directory with no local run directory or log for that run id. Maps the initiative id to
    # that run id. Absent when the newest run is local, is a fetched remote run, or there is no run.
    remote_unfetched: dict[str, str]
    missing_repos: NotRequired[list[str]]  # sorted repository paths newly seen missing this tick
    last_housekeeping_at: str | None  # ISO UTC of the newest recorded housekeeping action; None when the store has none
    housekeeping_hours: float  # period in hours; the edge fills it from profile chair.housekeeping_hours, default 24
    run_exited: dict[str, bool]  # initiative to whether its newest run is exited or quarantined in the run store; absent is False
    schema_deaths: dict[str, list[str]]  # initiative to its newest two run ids, present only when both exited and died on the schema-version cause
    # An entry means that run's host has been unreachable for at least ten minutes and the run has no exit record in the store.
    lost_runs: dict[str, str]
    # An entry only for a remote run whose host has a fresh beat and whose pid was probed; a stale host or a failed or timed-out probe has none.
    pid_probe: dict[str, PidProbeFact]
    empty_decompose: list[EmptyDecomposeFacts]  # decomposed intakes whose ended decompose run left zero stored task items
    # Each initiative's newest run's host, "" meaning the local machine; an initiative absent here has no run yet.
    newest_run_host: dict[str, str]
    # An initiative with unfinished work from its newest run (a carried partial phase or an approved task not yet
    # landed) maps to that run's host, "" meaning this machine; nothing consumes this yet.
    home: dict[str, str]
    review_prs: NotRequired[list[ReviewPr]]  # approved tasks awaiting a review PR; absent means none
    running: NotRequired[list[RunningInitiative]]  # initiatives with a live lane on any machine; absent means none
    # Keyed `<candidate>|<other>`: the count of consecutive prior deferrals for that ordered pair; absent means 0.
    steer_streaks: NotRequired[dict[str, int]]
    stranded: NotRequired[list[StrandedPhase]]  # phases with approved work not landed; absent means none
    phase_branches: NotRequired[list[PhaseBranch]]  # phase branches against main; absent means none


ActionKind = Literal[
    "standby",
    "take_lease",
    "fetch",
    "fetch_exit",
    "land",
    "land_phase",
    "clear_branches",
    "relaunch",
    "retry",
    "rescue",
    "needs_chair",
    "launch_epic",
    "launch_decompose",
    "pull",
    "housekeeping",
    "stale_to_draft",
    "mark_lost",
    "check_login",
    "stalled_usr1",
    "stalled_stop",
    "review_landed",
    "steer_clear",
    "carry_phase",
    "rebase_phase",
]


class Action(TypedDict, total=False):
    """mark_lost only records the loss to the chair's own action log: it is never an attempt against a
    task, and by construction never touches the attempts table, so it never counts toward
    harness_failures or any retry cap. check_login carries host, performs an ssh call and a store
    write, and like mark_lost never touches the attempts table. needs_chair carries either
    initiative and cause, or host and cause when the entry names a lane host rather than an
    initiative; when host is present cause is "login_lapsed" and there is no initiative key.
    review_landed carries initiative, phase, task_id, repo, url and merged_at. steer_clear carries
    initiative, other and paths: the launch of initiative is deferred because it shares paths with other.
    carry_phase carries initiative, phase, pr_branch and picks; rebase_phase carries initiative, phase, branch,
    tip and base."""

    kind: ActionKind
    epoch: int
    task_id: str
    repo: str
    run: str  # a land or fetch names the run that holds the approved record; a fetch_exit or mark_lost names the lost run; a land_phase names the run whose phase-mode land command performs it
    initiative: str  # retry, rescue and land_phase carry initiative (land_phase carries no task_id); a stale_to_draft, mark_lost, carry_phase or rebase_phase names the initiative
    phase: str  # a land_phase names the phase it lands; a carry_phase or rebase_phase names the phase it works on
    cause: str
    intake_ids: list[str]
    holder: str
    host: str
    until: str  # a standby names when the holder's takeover window ends
    reason: str  # a take_lease over an expired takeover says so; a stale_to_draft carries its stale_reason string
    stale_tasks: list[str]  # a stale_to_draft names the task ids found stale
    since: str  # a stale_to_draft names the ISO timestamp the staleness was detected
    carry: list[str]  # the phases a clear_branches keeps and merges main into
    url: str  # a review_landed names the review PR
    merged_at: str | None  # a review_landed names when that PR merged
    other: str  # a steer_clear names the running initiative it steers clear of
    paths: list[str]  # a steer_clear names the shared paths
    pr_branch: str  # a carry_phase names the branch its pull request is opened from
    picks: list[CarryTask]  # a carry_phase lists the commits to cherry-pick, in order
    branch: str  # a rebase_phase names the phase branch it rebases
    tip: str  # a rebase_phase names the tip being replaced, for the backup ref
    base: str  # a rebase_phase names the commit or ref it rebases onto


class PlanLands(Protocol):
    def __call__(self, facts: Facts) -> list[Action]:
        """plan_lands(facts) -> list[action]: land and clear_branches for approved tasks."""
        ...


class PlanRecover(Protocol):
    def __call__(self, facts: Facts) -> list[Action]:
        """plan_recover(facts) -> list[action]: relaunch, retry, rescue and needs_chair for quarantines."""
        ...


class PlanFill(Protocol):
    def __call__(self, facts: Facts, free_lanes: int, withheld: frozenset[str] = frozenset()) -> list[Action]:
        """plan_fill(facts, free_lanes, withheld) -> list[action]: launch_epic, launch_decompose and pull; withheld launch no epic."""
        ...


class PlanTick(Protocol):
    def __call__(self, facts: Facts, now: datetime | None = None) -> list[Action]:
        """plan_tick(facts, now) -> list[action]: the whole tick, lease first, then lands, recover and fill, then at most one housekeeping when now is given."""
        ...


def stamp(action: Action, epoch: int) -> Action:
    return {**action, "epoch": epoch}


def is_fenced(action: Action, current_epoch: int) -> bool:
    """True when the action was planned under a different lease epoch, or carries none."""
    return action.get("epoch") != current_epoch
