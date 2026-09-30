from agent_tools import queue_rows, route
from agent_tools.chair_read_docket import docket_from_builder, docket_from_rows, work_store_ready


def _item(id: str, state: str, needs: tuple[str, ...] = (), initiative: str = "alpha", phase: str = "p1") -> dict:
    return {"id": id, "initiative": initiative, "phase": phase, "state": state, "needs": list(needs), "file": f"{phase}/{id}.md"}


def _row(
    task_id: str,
    state: str,
    needs: tuple[str, ...] = (),
    initiative: str = "alpha",
    phase: str = "p1",
    holder: str | None = None,
    expires_at: str = "",
) -> dict:
    return {
        "kind": "task",
        "initiative": initiative,
        "task_id": task_id,
        "phase": phase,
        "state": state,
        "needs": list(needs),
        "title": task_id,
        "surfaces": [],
        "body": "",
        "extra": {},
        "holder": holder,
        "epoch": 0,
        "expires_at": expires_at,
    }


def _init(*ready: str) -> dict:
    return {"id": "alpha", "started": False, "ready_tasks": [{"id": t, "needs": []} for t in ready], "landed": set()}


def test_builder_output_maps_to_the_documented_keys():
    items = [_item("t0", "done"), _item("t1", "ready", ("t0",)), _item("t2", "ready", ("t9",)), _item("t3", "todo")]
    assert docket_from_builder(route.initiative_summaries(items), items, 2, 4) == {
        "initiatives": [
            {
                "id": "alpha",
                "started": True,
                "ready_tasks": [{"id": "t1", "needs": ["t0"], "requires": []}],
                "waiting_tasks": [{"id": "t2", "needs": ["t9"], "requires": []}],
                "landed": {"t0"},
            }
        ],
        "busy_lanes": 2,
        "max_in_flight": 4,
    }


def test_a_docket_with_one_ready_task_is_ready():
    assert work_store_ready({"initiatives": [_init(), _init("t1")], "busy_lanes": 0, "max_in_flight": 4}) is True


def test_a_docket_where_every_ready_tasks_list_is_empty_is_not_ready():
    assert work_store_ready({"initiatives": [_init(), _init()], "busy_lanes": 0, "max_in_flight": 4}) is False


def test_an_empty_docket_is_not_ready():
    assert work_store_ready({"initiatives": [], "busy_lanes": 0, "max_in_flight": 4}) is False


NOW = "2026-09-27T00:00:00Z"


def _alpha(
    ready: list[dict], started: bool = False, landed: frozenset = frozenset(), waiting: list[dict] | None = None
) -> list[dict]:
    return [{"id": "alpha", "started": started, "ready_tasks": ready, "waiting_tasks": waiting or [], "landed": set(landed)}]


def test_a_ready_row_with_all_needs_done_is_offered():
    rows = [_row("t0", "done"), _row("t1", "ready", ("t0",))]
    assert docket_from_rows(rows, NOW) == _alpha([{"id": "t1", "needs": ["t0"], "requires": []}], started=True, landed=frozenset({"t0"}))


def test_a_todo_row_is_never_offered_even_with_satisfied_needs():
    assert docket_from_rows([_row("t0", "done"), _row("t1", "todo", ("t0",))], NOW) == []


def test_a_ready_row_with_an_undone_need_is_not_offered_but_carried_as_waiting():
    """Before this, the whole initiative was omitted, so the chair could never report what it waits on."""
    assert docket_from_rows([_row("t0", "todo"), _row("t1", "ready", ("t0",))], NOW) == _alpha(
        [], waiting=[{"id": "t1", "needs": ["t0"], "requires": []}]
    )


def test_only_the_earliest_phase_with_a_ready_task_is_offered():
    rows = [_row("a1", "ready", phase="p1"), _row("b1", "ready", phase="p2")]
    assert docket_from_rows(rows, NOW) == _alpha([{"id": "a1", "needs": [], "requires": []}])


def test_a_ready_row_behind_a_blocked_earlier_phase_is_withheld():
    assert docket_from_rows([_row("a1", "blocked", phase="p1"), _row("b1", "ready", phase="p2")], NOW) == []


def test_a_claimed_row_with_a_live_lease_is_withheld():
    rows = [_row("t1", "ready", holder="lane-1", expires_at="2026-09-27T01:00:00Z")]
    assert docket_from_rows(rows, NOW) == _alpha([])


def test_the_same_row_is_offered_once_the_lease_expires():
    rows = [_row("t1", "ready", holder="lane-1", expires_at=NOW)]
    assert docket_from_rows(rows, NOW) == _alpha([{"id": "t1", "needs": [], "requires": []}])


def test_the_same_row_is_offered_once_the_holder_is_empty():
    rows = [_row("t1", "ready", holder=None, expires_at="2026-09-27T23:00:00Z")]
    assert docket_from_rows(rows, NOW) == _alpha([{"id": "t1", "needs": [], "requires": []}])


def _parsed_row(task_id: str, frontmatter: str) -> dict:
    row = queue_rows.parse_item("task", ("alpha", "p1", f"{task_id}.md"), f"---\n{frontmatter}---\nbody\n")
    assert row is not None
    return {**row, "holder": None, "epoch": 0, "expires_at": ""}


def test_a_stored_rows_requires_frontmatter_reaches_its_ready_task():
    rows = [_parsed_row("t1", "state: ready\nrequires: [go]\n"), _parsed_row("t2", "state: ready\n")]
    assert docket_from_rows(rows, NOW)[0]["ready_tasks"] == [
        {"id": "t1", "needs": [], "requires": ["go"]}, {"id": "t2", "needs": [], "requires": []},
    ]


def test_a_work_item_files_requires_frontmatter_reaches_its_ready_task():
    texts = {"t1": "---\nstate: ready\nrequires: [go]\n---\n", "t2": "---\nstate: ready\n---\n"}
    items = [
        route.work_item(route.parse_frontmatter(text)[0], initiative="alpha", phase_dir="p1", stem=stem)
        for stem, text in texts.items()
    ]
    assert docket_from_builder(route.initiative_summaries(items), items, 0, 4)["initiatives"][0]["ready_tasks"] == [
        {"id": "t1", "needs": [], "requires": ["go"]}, {"id": "t2", "needs": [], "requires": []},
    ]


def test_parity_the_same_board_as_rows_and_as_files_gives_the_same_initiatives():
    items = [
        _item("t0", "done"), _item("t1", "ready", ("t0",)), _item("t2", "ready", ("t9",)), _item("t3", "todo"),
        _item("t4", "ready", phase="p2"), _item("t5", "dropped", initiative="beta"), _item("t6", "ready", ("t5",), "beta"),
        _item("t7", "blocked", initiative="gamma"), _item("t8", "ready", initiative="gamma", phase="p2"),
        _item("t9", "approved", initiative="delta"),
    ]
    rows = [_row(i["id"], i["state"], tuple(i["needs"]), i["initiative"], i["phase"]) for i in items]
    assert docket_from_rows(rows, NOW) == docket_from_builder(route.initiative_summaries(items), items, 0, 4)["initiatives"]
