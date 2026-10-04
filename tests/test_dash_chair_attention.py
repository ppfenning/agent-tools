from agent_tools.dash_chair_attention import chair_attention


def test_counts_person_addressed_entries_and_drafts():
    inbox = [
        {"to": "alice", "ref": "coxswain://needs_chair/a"},
        {"to": "chair", "ref": "coxswain://needs_chair/b"},
        {"to": "bob", "ref": "coxswain://decision/c"},
        {"to": "chair-2", "ref": "coxswain://decision/d"},
        {"ref": "coxswain://decision/e"},
    ]
    assert chair_attention(inbox, ["draft-1", "draft-2"]) == {"needs_you": 2, "drafts": 2}


def test_empty_inputs_are_zero():
    assert chair_attention([], []) == {"needs_you": 0, "drafts": 0}
