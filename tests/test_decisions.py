from agent_tools.decisions import (
    filter_decisions,
    make_answer_entry,
    make_ask_entry,
    merge_decisions,
    open_decisions_newest_first,
    validate_answer,
    validate_options,
)


def test_validate_options_refuses_one_option():
    assert validate_options(["yes"]) is not None


def test_validate_options_accepts_two_options():
    assert validate_options(["yes", "no"]) is None


def test_make_ask_entry_shape():
    entry = make_ask_entry("ship it?", ["yes", "no"], "release gate", "cox", "d1", "2026-09-29T10:00:00Z")
    assert entry == {
        "ref": "coxswain://decision/d1",
        "from": "cox",
        "to": "pat",
        "note": "ship it?",
        "id": "d1",
        "ack": False,
        "options": ["yes", "no"],
        "context": "release gate",
        "asked_at": "2026-09-29T10:00:00Z",
    }


def test_validate_answer_refuses_unknown_decision():
    assert validate_answer(None, "yes") == "unknown decision id"


def test_validate_answer_refuses_already_answered():
    ask_entry = {"ack": True, "options": ["yes", "no"]}
    assert validate_answer(ask_entry, "yes") == "decision already answered"


def test_validate_answer_refuses_option_not_offered():
    ask_entry = {"ack": False, "options": ["yes", "no"]}
    assert validate_answer(ask_entry, "maybe") == "option not offered: maybe"


def test_validate_answer_accepts_valid_option():
    ask_entry = {"ack": False, "options": ["yes", "no"]}
    assert validate_answer(ask_entry, "yes") is None


def test_make_answer_entry_shape():
    ask_entry = {"ref": "coxswain://decision/d1", "id": "d1"}
    entry = make_answer_entry(ask_entry, "yes", "2026-09-29T11:00:00Z", "m1")
    assert entry == {
        "ref": "coxswain://decision/d1",
        "from": "pat",
        "to": "chair",
        "note": "answer: yes",
        "id": "m1",
        "ack": False,
        "answer": "yes",
        "asked_at": "2026-09-29T11:00:00Z",
    }


def test_merge_decisions_pairs_answer_and_leaves_unanswered_none():
    asks = [
        {
            "ref": "coxswain://decision/d1",
            "id": "d1",
            "note": "ship it?",
            "options": ["yes", "no"],
            "context": "release gate",
            "asked_at": "2026-09-29T10:00:00Z",
        },
        {
            "ref": "coxswain://decision/d2",
            "id": "d2",
            "note": "roll back?",
            "options": ["yes", "no"],
            "context": "incident",
            "asked_at": "2026-09-29T11:00:00Z",
        },
    ]
    answers = [{"ref": "coxswain://decision/d1", "answer": "yes"}]
    merged = merge_decisions(asks, answers)
    assert merged[0]["answer"] == "yes"
    assert merged[1]["answer"] is None


def test_filter_decisions_open_only_returns_unanswered():
    decisions = [{"id": "d1", "answer": "yes"}, {"id": "d2", "answer": None}]
    assert filter_decisions(decisions, open_only=True) == [{"id": "d2", "answer": None}]


def test_filter_decisions_rejects_both_flags():
    try:
        filter_decisions([], open_only=True, answered_only=True)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_filter_decisions_default_returns_input_unchanged():
    decisions = [{"id": "d1", "answer": "yes"}, {"id": "d2", "answer": None}]
    assert filter_decisions(decisions) == decisions


def test_open_decisions_newest_first_orders_and_drops_answer():
    decisions = [
        {
            "id": "d1",
            "question": "ship it?",
            "options": ["yes", "no"],
            "context": "release gate",
            "asked_at": "2026-09-29T10:00:00Z",
            "answer": None,
        },
        {
            "id": "d2",
            "question": "roll back?",
            "options": ["yes", "no"],
            "context": "incident",
            "asked_at": "2026-09-29T12:00:00Z",
            "answer": None,
        },
    ]
    result = open_decisions_newest_first(decisions)
    assert [d["id"] for d in result] == ["d2", "d1"]
    assert "answer" not in result[0]
