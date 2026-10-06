from agent_tools.queue_rows import render_item, row_path

ROW = {
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


def _lines(row: dict) -> list[str]:
    return render_item(row).split("\n")


def test_landed_with_extra_initiative() -> None:
    row = {**ROW, "state": "landed", "extra": {"initiative": "queue"}}
    assert "initiative: queue" in _lines(row)
    assert row_path(row) == "intake/idea.md"


def test_decomposed_with_extra_initiative() -> None:
    row = {**ROW, "state": "decomposed", "extra": {"initiative": "queue"}}
    assert "initiative: queue" in _lines(row)
    assert row_path(row) == "intake/idea.md"


def test_decomposed_whose_own_id_names_an_initiative() -> None:
    row = {**ROW, "state": "decomposed", "initiative": "queue", "task_id": "queue"}
    assert "initiative: queue" in _lines(row)


def test_extra_initiative_wins_over_own_id() -> None:
    row = {**ROW, "state": "landed", "initiative": "queue", "task_id": "queue", "extra": {"initiative": "other"}}
    assert [line for line in _lines(row) if line.startswith("initiative:")] == ["initiative: other"]


def test_landed_with_neither_renders_no_line() -> None:
    row = {**ROW, "state": "landed"}
    assert not [line for line in _lines(row) if line.startswith("initiative:")]


def test_queued_renders_no_line() -> None:
    row = {**ROW, "initiative": "queue", "task_id": "queue"}
    assert not [line for line in _lines(row) if line.startswith("initiative:")]
