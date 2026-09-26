from pathlib import Path

from agent_tools.draft_list import DraftRow, count_drafts, format_age, list_drafts, read_drafts

NOW = "2026-09-26T12:00:00Z"


def test_draft_true_is_listed_and_false_or_absent_is_not():
    fms = [
        {"id": "a", "draft": True, "proposed_by": "steward", "proposed_at": "2026-09-26T09:00:00Z"},
        {"id": "b", "draft": False},
        {"id": "c"},
    ]
    assert list_drafts(fms, NOW) == [DraftRow("a", "steward", 10800)]


def test_string_true_from_the_route_parser_is_a_draft():
    assert [r.id for r in list_drafts([{"id": "a", "draft": "true"}, {"id": "b", "draft": "false"}], NOW)] == ["a"]


def test_oldest_first_then_id_with_undated_last():
    fms = [
        {"id": "new", "draft": True, "proposed_at": "2026-09-26T11:00:00Z"},
        {"id": "undated", "draft": True},
        {"id": "b-old", "draft": True, "proposed_at": "2026-09-24T12:00:00Z"},
        {"id": "a-old", "draft": True, "proposed_at": "2026-09-24T12:00:00Z"},
    ]
    assert [r.id for r in list_drafts(fms, NOW)] == ["a-old", "b-old", "new", "undated"]


def test_missing_proposer_shows_unknown():
    assert list_drafts([{"id": "a", "draft": True}], NOW)[0].proposed_by == "unknown"


def test_bad_or_missing_timestamp_gives_age_none():
    fms = [{"id": "a", "draft": True, "proposed_at": "yesterday"}, {"id": "b", "draft": True}]
    assert [r.age_seconds for r in list_drafts(fms, NOW)] == [None, None]


def test_format_age_renders_hours_days_and_unknown():
    assert [format_age(s) for s in (10800, 172800, None, 60)] == ["3h", "2d", "?", "<1h"]


def test_count_drafts():
    assert count_drafts([DraftRow("a", "steward", 1), DraftRow("b", "gate", None)]) == 2


def test_read_drafts_reads_the_work_directory(tmp_path: Path):
    def initiative(name: str, header: str) -> None:
        (tmp_path / name).mkdir()
        (tmp_path / name / "initiative.md").write_text("---\n" + header + "\n---\n\nbody\n")

    initiative("old", "draft: true\nproposed_by: steward\nproposed_at: 2026-09-24T12:00:00Z")
    initiative("live", "title: live")
    initiative("fresh", "draft: true\nproposed_by: gate\nproposed_at: 2026-09-26T11:00:00Z")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "initiative.md").write_bytes(b"\xff\xfe\x00")
    assert read_drafts(tmp_path, NOW) == [DraftRow("old", "steward", 172800), DraftRow("fresh", "gate", 3600)]
