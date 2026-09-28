from agent_tools import intake_state

_INTAKE_NAMED = "---\nid: a\nintake: intake/a.md\n---\nbody\n"


def test_intake_group_calls_an_intake_named_by_an_open_initiative_decomposed():
    entry = {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"}
    initiatives = [{"id": "x", "done": False, "text": _INTAKE_NAMED}]
    by_id = {i["id"]: i for i in initiatives}
    assert intake_state.intake_group(entry, by_id, initiatives) == "decomposed"


def test_intake_group_calls_an_intake_named_by_a_done_initiative_landed():
    entry = {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"}
    initiatives = [{"id": "x", "done": True, "text": _INTAKE_NAMED}]
    by_id = {i["id"]: i for i in initiatives}
    assert intake_state.intake_group(entry, by_id, initiatives) == "landed"


def test_intake_group_keeps_an_intake_named_by_no_initiative_queued():
    entry = {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"}
    initiatives = [{"id": "x", "done": False, "text": "---\nintake: intake/b.md\n---\n"}]
    by_id = {i["id"]: i for i in initiatives}
    assert intake_state.intake_group(entry, by_id, initiatives) == "queued"


def test_intake_group_falls_through_to_the_substring_rule_when_done_and_uncited():
    entry = {"id": "a", "title": "A", "initiative": None, "done": True, "path": "intake/a.md"}
    initiatives = [{"id": "x", "done": False, "text": "---\nintake: intake/b.md\n---\n"}]
    by_id = {i["id"]: i for i in initiatives}
    assert intake_state.intake_group(entry, by_id, initiatives) == "landed"


def test_intake_group_falls_through_to_the_substring_rule_when_done_and_cited():
    entry = {"id": "a", "title": "A", "initiative": None, "done": True, "path": "intake/a.md"}
    initiatives = [{"id": "x", "done": False, "text": "See intake/a.md for background."}]
    by_id = {i["id"]: i for i in initiatives}
    assert intake_state.intake_group(entry, by_id, initiatives) == "decomposed"


def test_intake_field_reads_the_first_intake_line_quotes_stripped():
    assert intake_state.intake_field('---\nintake: "intake/a.md"\n---\nbody\n') == "intake/a.md"


def test_intake_field_is_none_with_no_frontmatter_block():
    assert intake_state.intake_field("no frontmatter here") is None


def test_intake_field_is_none_with_no_intake_line():
    assert intake_state.intake_field("---\nid: a\n---\nbody\n") is None
