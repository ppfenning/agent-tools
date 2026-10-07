from agent_tools.chair_widen_ticket import WIDEN_MARK, widen_ticket_text, widening_count

WHEN = "2026-10-06"
TAIL = "Add that one named addition and change nothing else in the file."
NOTE = f"Widened by the chair 2026-10-06: src/app.rs is now in surfaces for accessor only. {TAIL}"
ADD = [("src/app.rs", "accessor")]


def test_ready_ticket_gains_path_state_and_note():
    text = "---\nstate: ready\nsurfaces: [agent_tools/a.py]\n---\nDo the thing.\n"
    assert widen_ticket_text(text, ADD, WHEN) == (
        f"---\nstate: ready\nsurfaces: [agent_tools/a.py, src/app.rs]\n---\nDo the thing.\n\n{NOTE}\n",
        None,
    )


def test_path_already_in_surfaces_is_not_duplicated_but_still_noted():
    text = "---\nstate: blocked\nsurfaces: [agent_tools/a.py, src/app.rs]\n---\nBody.\n"
    new, _ = widen_ticket_text(text, ADD, WHEN)
    assert new == f"---\nstate: ready\nsurfaces: [agent_tools/a.py, src/app.rs]\n---\nBody.\n\n{NOTE}\n"
    assert widening_count(new) == 1


def test_blocked_ticket_becomes_ready():
    new, _ = widen_ticket_text("---\nstate: blocked\nsurfaces: [a.py]\n---\nBody.\n", ADD, WHEN)
    assert new is not None and new.startswith("---\nstate: ready\n")


def test_done_ticket_refuses_and_returns_no_text():
    text = "---\nstate: done\nsurfaces: [a.py]\n---\nBody.\n"
    assert widen_ticket_text(text, ADD, WHEN) == (None, "widen: work item state is 'done', not widening")


def test_dropped_ticket_refuses():
    text = "---\nstate: dropped\nsurfaces: [a.py]\n---\nBody.\n"
    assert widen_ticket_text(text, ADD, WHEN) == (None, "widen: work item state is 'dropped', not widening")


def test_block_list_surfaces_is_handled():
    text = "---\nstate: ready\nsurfaces:\n  - agent_tools/a.py\nowner: x\n---\nBody.\n"
    new, _ = widen_ticket_text(text, ADD, WHEN)
    assert new == f"---\nstate: ready\nsurfaces:\n  - agent_tools/a.py\n  - src/app.rs\nowner: x\n---\nBody.\n\n{NOTE}\n"


def test_unindented_block_list_keeps_its_indent():
    new, _ = widen_ticket_text("---\nstate: ready\nsurfaces:\n- a.py\n---\nBody.\n", ADD, WHEN)
    assert new is not None and new.startswith("---\nstate: ready\nsurfaces:\n- a.py\n- src/app.rs\n---\n")


def test_empty_flow_list_gains_path():
    new, _ = widen_ticket_text("---\nstate: ready\nsurfaces: []\n---\n", ADD, WHEN)
    assert new == f"---\nstate: ready\nsurfaces: [src/app.rs]\n---\n{NOTE}\n"


def test_wrapped_flow_list_and_comments_are_read():
    text = "---\nstate: ready  # live\nsurfaces: [a.py,\n  'b, c.py']  # two\n---\nBody.\n"
    new, _ = widen_ticket_text(text, [("b, c.py", "accessor")], WHEN)
    assert new is not None
    assert new.startswith("---\nstate: ready  # live\nsurfaces: [a.py, 'b, c.py']  # two\n---\n")


def test_multiple_additions_each_get_a_note():
    new, _ = widen_ticket_text("---\nstate: ready\nsurfaces: [a.py]\n---\nBody.\n", [("x.py", "f"), ("y.py", "g")], WHEN)
    assert new is not None and "surfaces: [a.py, x.py, y.py]\n" in new and widening_count(new) == 2


def test_no_frontmatter_refuses():
    assert widen_ticket_text("Body only.\n", ADD, WHEN) == (None, "widen: work item has no frontmatter")


def test_no_surfaces_key_refuses():
    assert widen_ticket_text("---\nstate: ready\n---\nBody.\n", ADD, WHEN) == (None, "widen: work item has no surfaces key")


def test_scalar_surfaces_refuses_by_name():
    text = "---\nstate: ready\nsurfaces: a.py\n---\nBody.\n"
    assert widen_ticket_text(text, ADD, WHEN) == (None, "widen: work item surfaces is 'a.py', not a list")


def test_no_state_line_refuses():
    assert widen_ticket_text("---\nsurfaces: [a.py]\n---\nBody.\n", ADD, WHEN) == (None, "widen: work item has no state field")


def test_empty_or_multiline_addition_refuses():
    text = "---\nstate: ready\nsurfaces: [a.py]\n---\nBody.\n"
    assert widen_ticket_text(text, [], WHEN) == (None, "widen: no additions named, nothing to widen")
    assert widen_ticket_text(text, [("x.py", f"f\n{WIDEN_MARK}")], WHEN)[0] is None


def test_widening_count_two_notes_and_none():
    two = f"---\nstate: ready\nsurfaces: []\n---\nBody.\n\n{WIDEN_MARK} a: x.\n\n{WIDEN_MARK} b: y.\n"
    assert widening_count(two) == 2
    assert widening_count("---\nstate: ready\nsurfaces: []\n---\nBody.\n") == 0
