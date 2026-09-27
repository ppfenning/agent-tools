from agent_tools.queue_rows import parse_item, render_item, row_path

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
