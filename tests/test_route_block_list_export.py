import yaml

from agent_tools import route
from agent_tools.queue_rows import render_item


def test_parse_frontmatter_reads_the_unindented_block_lists_yaml_writes():
    text = "---\nid: t\nneeds:\n- a-task\n- b-task\nsurfaces:\n- agent_tools/x.py\n- tests/test_x.py (new)\nstate: ready\n---\nBody.\n"
    fields, body = route.parse_frontmatter(text)
    assert fields["needs"] == ["a-task", "b-task"]
    assert fields["surfaces"] == ["agent_tools/x.py", "tests/test_x.py (new)"]
    assert fields["state"] == "ready"
    assert body.strip() == "Body."


def test_parse_frontmatter_still_reads_indented_block_lists():
    fields, _ = route.parse_frontmatter("---\nneeds:\n  - a\n  - b\nid: t\n---\n")
    assert fields["needs"] == ["a", "b"] and fields["id"] == "t"


def test_an_exported_ticket_round_trips_its_needs_and_surfaces():
    # The store's export renders through yaml.safe_dump; the file reader must see the same lists back.
    row = {"kind": "task", "initiative": "i", "phase": "p", "task_id": "i-t", "state": "ready", "title": "T: with a colon",
           "needs": ["i-a", "i-b"], "surfaces": ["agent_tools/route.py", "tests/test_y.py (new)"], "extra": {}, "body": "B\n"}
    fields, _ = route.parse_frontmatter(render_item(row))
    assert fields["needs"] == ["i-a", "i-b"]
    assert fields["surfaces"] == ["agent_tools/route.py", "tests/test_y.py (new)"]


def test_a_yaml_quoted_item_is_unquoted():
    text = "---\n" + yaml.safe_dump({"surfaces": ["it's: quoted", "plain"]}, sort_keys=False) + "---\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["surfaces"] == ["it's: quoted", "plain"]
