from agent_tools.chair_widen import _paths, classify_handoff

SURFACES = ["src/ui/regatta.rs", "src/input.rs (new)"]


def test_accessor_only_handoff_gives_one_pair() -> None:
    reason = "src/app.rs is out of bounds, I need an accessor for the selected frame"
    assert classify_handoff(reason, SURFACES) == [("src/app.rs", "accessor")]


def test_pub_crate_ask_gives_pub_crate() -> None:
    reason = "src/app.rs needs pub(crate) on selected_frame."
    assert classify_handoff(reason, SURFACES) == [("src/app.rs", "pub(crate)")]


def test_design_question_gives_none() -> None:
    reason = "src/app.rs needs an accessor, should we add one?"
    assert classify_handoff(reason, SURFACES) is None


def test_three_outside_files_gives_none() -> None:
    reason = "I need an accessor in src/a.rs. A getter in src/b.rs. A flag in src/c.rs."
    assert classify_handoff(reason, SURFACES) is None


def test_only_surface_files_gives_none() -> None:
    reason = "src/input.rs needs a new constant and src/ui/regatta.rs a flag."
    assert classify_handoff(reason, SURFACES) is None


def test_new_file_ask_gives_none() -> None:
    reason = "I need a new file src/frame.rs with a constant."
    assert classify_handoff(reason, SURFACES) is None


def test_unsmall_ask_sends_whole_handoff_to_none() -> None:
    reason = "src/app.rs needs an accessor. src/feed.rs needs its parser rewritten."
    assert classify_handoff(reason, SURFACES) is None


def test_two_outside_files_give_two_pairs() -> None:
    reason = "src/app.rs needs an accessor for the frame. src/feed.rs needs a new field."
    assert classify_handoff(reason, SURFACES) == [("src/app.rs", "accessor"), ("src/feed.rs", "field")]


def test_paths_strip_sentence_period_and_ignore_abbreviations() -> None:
    assert _paths("src/app.rs. Then x/y.py, e.g. later") == ["src/app.rs", "x/y.py"]
