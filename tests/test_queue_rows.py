from agent_tools.queue_rows import intake_stamp, parse_item, plan_to_rows, render_item, row_path

TASK = "---\nstate: doing\ntitle: 'Codec: rows'\nneeds:\n- a-1\nsurfaces:\n- agent_tools/x.py\nrepo: coxswain\n---\nBody line.\n"
TASK_ROW = {
    "kind": "task",
    "initiative": "queue",
    "task_id": "codec",
    "phase": "q",
    "state": "doing",
    "needs": ["a-1"],
    "title": "Codec: rows",
    "surfaces": ["agent_tools/x.py"],
    "body": "Body line.\n",
    "extra": {"repo": "coxswain"},
}
INTAKE = "---\ntitle: Idea\n---\nSome idea.\n"
INTAKE_ROW = {
    "kind": "intake",
    "initiative": "intake",
    "task_id": "idea",
    "phase": "",
    "state": "queued",
    "needs": [],
    "title": "Idea",
    "surfaces": [],
    "body": "Some idea.\n",
    "extra": {},
}


def test_task_parse() -> None:
    assert parse_item("task", ("queue", "q", "codec.md"), TASK) == TASK_ROW


def test_task_without_state_defaults_to_todo() -> None:
    assert parse_item("task", ("queue", "q", "codec.md"), "---\ntitle: T\n---\n")["state"] == "todo"


def test_intake_parse_root() -> None:
    assert parse_item("intake", ("idea.md",), INTAKE) == INTAKE_ROW


def test_intake_parse_done() -> None:
    assert parse_item("intake", ("done", "idea.md"), INTAKE) == {**INTAKE_ROW, "state": "done"}


def test_extra_keys_kept() -> None:
    row = parse_item("task", ("queue", "q", "codec.md"), "---\nstate: todo\nphase: other\nattempts:\n- {run: r1}\n---\n")
    assert row["extra"] == {"phase": "other", "attempts": [{"run": "r1"}]}


def test_scalars_kept_as_written() -> None:
    row = parse_item("task", ("queue", "q", "codec.md"), "---\ntitle: yes\nrepo: 1.10\ncreated: 2026-02-30\n---\n")
    assert (row["title"], row["extra"]) == ("yes", {"repo": "1.10", "created": "2026-02-30"})


def test_present_but_empty_keys_take_defaults() -> None:
    row = parse_item("task", ("queue", "q", "codec.md"), "---\ntitle:\nstate:\nneeds:\nsurfaces:\n---\n")
    assert (row["title"], row["state"], row["needs"], row["surfaces"]) == ("", "todo", [], [])


def test_render_then_parse_round_trip() -> None:
    parts = ("queue", "q", "codec.md")
    task = parse_item("task", parts, TASK)
    odd = parse_item("task", parts, "---\ntitle: yes\nrepo: 1.10\ncreated: 2026-02-30\nnote: ''\n---\n")
    intake = parse_item("intake", ("done", "idea.md"), INTAKE + "\n---\ntrailing rule\n")
    assert parse_item("task", parts, render_item(task)) == task
    assert parse_item("task", parts, render_item(odd)) == odd
    assert parse_item("intake", ("done", "idea.md"), render_item(intake)) == intake


def test_render_key_order() -> None:
    assert render_item(TASK_ROW) == TASK


def test_row_path_task() -> None:
    assert row_path(TASK_ROW) == "work/queue/q/codec.md"


def test_row_path_intake_root() -> None:
    assert row_path(INTAKE_ROW) == "intake/idea.md"


def test_row_path_intake_done() -> None:
    assert row_path({**INTAKE_ROW, "state": "done"}) == "intake/done/idea.md"


def test_malformed_file_gives_none() -> None:
    parts = ("queue", "q", "codec.md")
    assert parse_item("task", parts, "no frontmatter here\n") is None
    assert parse_item("task", parts, "---\ntitle: T\nno closing fence\n") is None
    assert parse_item("task", parts, "---\ntitle: [unclosed\n---\n") is None
    assert parse_item("task", parts, "---\n- just\n- a list\n---\n") is None
    assert parse_item("task", parts, "") is None
    assert parse_item("task", ("codec.md",), TASK) is None


PLAN = {
    "id": "queue",
    "title": "Queue",
    "body": "Move the queue.\n",
    "phases": [{"id": "q", "goal": "rows"}, {"id": "r", "goal": "read"}],
    "tasks": [
        {"id": "a-1", "phase": "q", "title": "First", "body": "One.\n", "needs": [], "surfaces": ["agent_tools/a.py"]},
        {"id": "a-2", "phase": "q", "title": "Second", "body": "Two.\n", "needs": ["a-1"], "surfaces": []},
        {"id": "a-3", "phase": "r", "title": "Third", "body": "Three.\n", "needs": [], "surfaces": ["x.py", "y.py"]},
    ],
}


def _task_row(task_id, phase, state, needs, title, surfaces, body) -> dict:
    return {
        "kind": "task", "initiative": "queue", "task_id": task_id, "phase": phase, "state": state,
        "needs": needs, "title": title, "surfaces": surfaces, "body": body, "extra": {"id": task_id, "phase": phase},
    }


def test_plan_to_rows_three_tasks() -> None:
    assert plan_to_rows(PLAN, "idea") == [
        {
            "kind": "initiative", "initiative": "queue", "task_id": "queue", "phase": "", "state": "todo",
            "needs": [], "title": "Queue", "surfaces": [], "body": "Move the queue.\n",
            "extra": {
                "id": "queue", "intake": "intake/idea.md", "phases": [{"id": "q", "goal": "rows"}, {"id": "r", "goal": "read"}],
            },
        },
        _task_row("a-1", "q", "ready", [], "First", ["agent_tools/a.py"], "One.\n"),
        _task_row("a-2", "q", "todo", ["a-1"], "Second", [], "Two.\n"),
        _task_row("a-3", "r", "ready", [], "Third", ["x.py", "y.py"], "Three.\n"),
    ]


INITIATIVE = "---\ntitle: Queue\nid: queue\nintake: intake/idea.md\nphases:\n- id: q\n  goal: rows\n- id: r\n  goal: read\n---\nMove the queue.\n"


def test_initiative_row_is_its_initiative_file() -> None:
    row = plan_to_rows(PLAN, "idea")[0]
    assert row_path(row) == "work/queue/initiative.md"
    assert render_item(row) == INITIATIVE
    assert parse_item("initiative", ("queue", "initiative.md"), INITIATIVE) == row


def test_intake_stamp_sets_initiative_and_keeps_original() -> None:
    row = parse_item("intake", ("idea.md",), INTAKE)
    stamped_file = "---\ntitle: Idea\ninitiative: queue\n---\nSome idea.\n"
    stamped = intake_stamp(row, "queue")
    assert stamped == parse_item("intake", ("done", "idea.md"), stamped_file)
    assert (stamped["extra"], stamped["initiative"], stamped["state"]) == ({"initiative": "queue"}, "intake", "done")
    assert row_path(stamped) == "intake/done/idea.md"
    assert row == INTAKE_ROW


def test_plan_row_equals_parsed_file_row() -> None:
    # Frontmatter as route.initiative_files writes it: id, phase, state, needs, surfaces, title.
    todo = "---\nid: a-2\nphase: q\nstate: todo\nneeds:\n- a-1\nsurfaces: []\ntitle: Second\n---\nTwo.\n"
    ready = "---\nid: a-1\nphase: q\nstate: ready\nneeds: []\nsurfaces:\n- agent_tools/a.py\ntitle: First\n---\nOne.\n"
    rows = plan_to_rows(PLAN, "idea")
    assert rows[2] == parse_item("task", ("queue", "q", "a-2.md"), todo)
    assert rows[1] == parse_item("task", ("queue", "q", "a-1.md"), ready)
    assert parse_item("task", ("queue", "q", "a-2.md"), render_item(rows[2])) == rows[2]
