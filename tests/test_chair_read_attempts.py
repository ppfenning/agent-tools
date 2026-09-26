from agent_tools.chair_read_attempts import attempt_rows, fill_causes, path_parts, read_attempts

PATH = "work/init-a/phase-1/task-x.md"


def test_attempts_on_different_items_sort_oldest_first():
    items = [
        (PATH, [{"run": "r2", "ts": "2026-09-02T00:00:00Z"}]),
        ("work/init-b/phase-2/task-y.md", [{"run": "r1", "ts": "2026-09-01T00:00:00Z"}]),
    ]
    assert [row["run"] for row in attempt_rows(items)] == ["r1", "r2"]


def test_a_row_takes_initiative_phase_and_task_from_the_path():
    (row,) = attempt_rows([(PATH, [{"run": "r1", "ts": "t"}])])
    assert (row["initiative"], row["phase"], row["task"]) == ("init-a", "phase-1", "task-x")


def test_an_explicit_initiative_is_kept():
    (row,) = attempt_rows([(PATH, [{"run": "r1", "ts": "t", "initiative": "other"}])])
    assert row["initiative"] == "other"


def test_an_item_with_no_attempts_adds_no_rows():
    assert attempt_rows([(PATH, []), (PATH, None)]) == []


def test_a_missing_cause_is_carried_as_absent():
    (row,) = attempt_rows([(PATH, [{"run": "r1", "ts": "t"}])])
    assert "cause" in row and row["cause"] is None


def test_a_path_outside_the_work_layout_has_no_parts():
    assert path_parts("notes/x.md") == (None, None, None)


def test_the_edge_loads_items_from_frontmatter(tmp_path):
    item = tmp_path / "work" / "init-a" / "phase-1" / "task-x.md"
    item.parent.mkdir(parents=True)
    item.write_text("---\nattempts:\n  - run: r1\n    ts: '2026-09-01'\n    cause: flaky\n---\nbody\n")
    (row,) = read_attempts(tmp_path)
    assert (row["run"], row["cause"], row["initiative"], row["path"]) == ("r1", "flaky", "init-a", str(item.relative_to(tmp_path)))


def test_a_file_attempt_with_no_cause_takes_the_store_rows_cause():
    store = [{"run_id": "r1", "task_id": "task-x", "seq": 1, "cause": "harness"}]
    (row,) = fill_causes([{"run": "r1", "task": "task-x", "cause": None}], store)
    assert row["cause"] == "harness"


def test_a_file_cause_is_kept_and_the_newest_seq_wins():
    store = [{"run_id": "r1", "task_id": "t", "seq": 2, "cause": "new"}, {"run_id": "r1", "task_id": "t", "seq": 1, "cause": "old"}]
    rows = fill_causes([{"run": "r1", "task": "t", "cause": None}, {"run": "r1", "task": "t", "cause": "file"}], store)
    assert [r["cause"] for r in rows] == ["new", "file"]
