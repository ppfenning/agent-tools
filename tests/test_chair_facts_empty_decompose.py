from agent_tools.chair_facts import empty_decompose_facts


def test_a_decomposed_intake_with_zero_items_and_an_ended_run_appears():
    facts = empty_decompose_facts(decomposed={"i": "i-decompose-1"}, live=set(), item_counts={"i": 0})
    assert facts == [{"initiative": "i", "run": "i-decompose-1"}]


def test_a_live_decompose_run_is_not_reported_even_with_zero_items():
    facts = empty_decompose_facts(decomposed={"i": "i-decompose-1"}, live={"i"}, item_counts={"i": 0})
    assert facts == []


def test_one_or_more_stored_items_is_not_reported():
    facts = empty_decompose_facts(decomposed={"i": "i-decompose-1"}, live=set(), item_counts={"i": 2})
    assert facts == []
