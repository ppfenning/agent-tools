from copy import deepcopy

from agent_tools.notify_core import KINDS, Event, NotifyConfig, config_from_profile, due, render


def _event(key="k1", body="b"):
    return Event("stall", key, "title", body, "")


def test_kinds_literal():
    assert KINDS == ("needs_chair", "host_login", "stall", "release_cut", "decision_card")


def test_config_missing_is_none():
    assert config_from_profile({}) is None


def test_config_empty_is_none():
    assert config_from_profile({"notify": {"ntfy": ""}}) is None


def test_config_non_string_is_none():
    assert config_from_profile({"notify": {"ntfy": 5}}) is None


def test_config_set():
    assert config_from_profile({"notify": {"ntfy": "https://ntfy.sh/pat-coxswain"}}) == NotifyConfig("https://ntfy.sh/pat-coxswain")


def test_render_truncates_long_body_with_ellipsis():
    title, body, click = render(Event("stall", "k", "t" * 80 + "\nsecond", "x" * 250, "https://x"))
    assert (len(title), len(body), body[-1], click) == (60, 200, "…", "https://x")


def test_due_drops_at_3599_and_sends_at_3600():
    e = _event()
    assert due([e], {"k1": 0.0}, 3599.0)[0] == []
    assert due([e], {"k1": 0.0}, 3600.0)[0] == [e]


def test_due_sends_repeated_key_once():
    sent, new = due([_event(body="a"), _event(body="b")], {}, 100.0)
    assert (sent, new) == ([_event(body="a")], {"k1": 100.0})


def test_due_prunes_old_entries():
    assert due([], {"old": 0.0, "recent": 3000.0}, 3600.0)[1] == {"recent": 3000.0}


def test_due_leaves_input_untouched():
    sent = {"k1": 0.0, "k2": 5000.0}
    before = deepcopy(sent)
    due([_event("k3")], sent, 5001.0)
    assert sent == before
