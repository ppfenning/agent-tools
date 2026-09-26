"""The contract every chair-loop planner reads: the facts a tick takes and the actions it returns.

Pure shapes and two pure helpers. No I/O and no harness or store imports. The edge gathers
the facts and executes the actions; the planners in between are pure.
"""
from typing import Literal, Protocol, TypedDict


class LeaseFacts(TypedDict):
    holder: str
    host: str
    epoch: int
    mine: bool
    released: bool
    stale: bool


class LimitsFacts(TypedDict):
    """Filled in by the edge from a pacing.assess result; nothing here is computed. launch_cap is the verdict tier ceiling."""

    hard_stop: bool
    weekly_fraction: float
    hard_stop_fraction: float
    launch_cap: int
    go_degraded: bool
    five_hour_fraction: float | None


class HostLanes(TypedDict):
    name: str
    live_runs: int


class DispatchFacts(TypedDict):
    """live_runs counts the local machine's live lanes; hosts holds each profile lane host in profile order."""

    max_in_flight: int
    live_runs: int
    hosts: list[HostLanes]


class ApprovedTask(TypedDict):
    id: str
    initiative: str
    repo: str
    phase: str
    phase_done: bool
    needs: list[str]
    run: str  # the newest run whose task record is approved and unlanded; empty when none is


class ReadyTask(TypedDict):
    id: str
    needs: list[str]


class InitiativeFacts(TypedDict):
    id: str
    started: bool
    ready_tasks: list[ReadyTask]
    landed: set[str]


class QuarantineFacts(TypedDict):
    task_id: str
    initiative: str
    cause: str
    harness_failures: int  # counted from run history by the edge
    has_patch: bool  # the task record in the store holds a non-blank build.patch
    rescue_failed: bool  # an earlier rescue_failed attempt exists for this task on the current ticket version


class Facts(TypedDict):
    lease: LeaseFacts
    limits: LimitsFacts
    dispatch: DispatchFacts
    approved: list[ApprovedTask]
    initiatives: list[InitiativeFacts]
    quarantines: list[QuarantineFacts]
    intake: list[str]  # oldest first
    work_store_ready: bool
    sources_configured: bool


ActionKind = Literal[
    "standby",
    "take_lease",
    "land",
    "clear_branches",
    "relaunch",
    "retry",
    "rescue",
    "needs_chair",
    "launch_epic",
    "launch_decompose",
    "pull",
]


class Action(TypedDict, total=False):
    kind: ActionKind
    epoch: int
    task_id: str
    repo: str
    run: str  # a land names the run that holds the approved record
    initiative: str  # retry and rescue carry initiative and task_id
    cause: str
    intake_ids: list[str]
    holder: str
    host: str


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
    def __call__(self, facts: Facts) -> list[Action]:
        """plan_tick(facts) -> list[action]: the whole tick, lease first, then lands, recover and fill."""
        ...


def stamp(action: Action, epoch: int) -> Action:
    return {**action, "epoch": epoch}


def is_fenced(action: Action, current_epoch: int) -> bool:
    """True when the action was planned under a different lease epoch, or carries none."""
    return action.get("epoch") != current_epoch
