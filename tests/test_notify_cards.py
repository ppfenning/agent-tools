import json

from agent_tools.notify_cards import card_events, pending_cards
from agent_tools.notify_core import BODY_MAX, TITLE_MAX


def _card(card_id, question="Ship it?", **extra):
    return {"id": card_id, "question": question, "options": ["ship", "hold"], "context": "c",
            "asked_at": "2026-10-06T10:00:00Z"} | extra


def test_one_pending_card_gives_one_event():
    (event,) = card_events([_card("d-1")])
    assert (event.kind, event.key, event.title, event.body, event.click) == (
        "decision_card", "decision_card:d-1", "Ship it?", "Ship it?", "")


def test_an_answered_card_gives_no_event():
    assert card_events([_card("d-1", answer="hold")]) == []


def test_two_pending_cards_give_two_distinct_keys():
    events = card_events([_card("d-1"), _card("d-2")])
    assert [e.key for e in events] == ["decision_card:d-1", "decision_card:d-2"]


def test_a_long_question_is_cut_as_render_cuts_it():
    (event,) = card_events([_card("d-1", "x" * 300 + "\nsecond line")])
    assert (len(event.body), event.body[-1], event.title) == (BODY_MAX, "…", "x" * TITLE_MAX)


_BUS = {"from": "chair", "to": "pat", "ack": False, "options": ["ship", "hold"], "context": "c"}
_ASK_1 = _BUS | {"ref": "coxswain://decision/d-1", "id": "d-1", "note": "Ship it?", "asked_at": "2026-10-06T10:00:00Z"}
_ASK_2 = _BUS | {"ref": "coxswain://decision/d-2", "id": "d-2", "note": "Hold it?", "asked_at": "2026-10-06T09:00:00Z"}
_ANSWER_2 = {"ref": "coxswain://decision/d-2", "id": "a-2", "from": "pat", "to": "chair", "note": "answer: hold",
             "ack": False, "answer": "hold", "asked_at": "2026-10-06T11:00:00Z"}


def _write(tmp_path, lines):
    (tmp_path / "courier.jsonl").write_text("".join(f"{line}\n" for line in lines))


def test_an_answered_ask_in_the_store_gives_no_event(tmp_path):
    _write(tmp_path, [json.dumps(e) for e in (_ASK_1, _ASK_2, _ANSWER_2)])
    assert [e.key for e in card_events(pending_cards(tmp_path))] == ["decision_card:d-1"]


def test_pending_cards_is_empty_without_a_store(tmp_path):
    assert pending_cards(tmp_path) == []


def test_a_torn_or_non_object_line_keeps_the_valid_cards(tmp_path):
    _write(tmp_path, [json.dumps(_ASK_1), "{not json", "5", "[1]", '"x"'])
    assert [c["id"] for c in pending_cards(tmp_path)] == ["d-1"]


def test_only_bad_lines_give_no_cards(tmp_path):
    _write(tmp_path, ["{not json", "[1]"])
    assert pending_cards(tmp_path) == []
