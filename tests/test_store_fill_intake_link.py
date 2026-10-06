from agent_tools import store_fill

_INTAKE = {
    "kind": "intake", "initiative": "", "task_id": "alpha", "phase": "", "state": "queued", "needs": [],
    "title": "A", "surfaces": [], "body": "", "extra": {},
}
_INITIATIVE = {
    "kind": "initiative", "initiative": "alpha", "task_id": "alpha", "phase": "", "state": "todo", "needs": [],
    "title": "Alpha", "surfaces": [], "body": "why", "extra": {"id": "alpha"},
}


def test_an_id_naming_an_initiative_is_decomposed_and_linked():
    assert store_fill.intake_state("alpha", frozenset({"alpha"})) == ("decomposed", {"initiative": "alpha"})


def test_an_id_naming_no_initiative_stays_queued_without_a_link():
    state, extra = store_fill.intake_state("beta", frozenset({"alpha"}))
    assert (state, "initiative" in extra) == ("queued", False)


def test_initiative_ids_takes_only_initiative_rows():
    assert store_fill.initiative_ids([_INITIATIVE, {**_INTAKE, "task_id": "x"}]) == frozenset({"alpha"})


def test_plan_fill_inserts_a_matching_intake_as_decomposed():
    (write,) = store_fill.plan_fill([_INTAKE], [_INITIATIVE]).writes
    assert (write["state"], write["extra"]["initiative"]) == ("decomposed", "alpha")


def test_plan_fill_inserts_an_unmatched_intake_as_queued():
    (write,) = store_fill.plan_fill([{**_INTAKE, "task_id": "beta"}], [_INITIATIVE]).writes
    assert (write["state"], "initiative" in write["extra"]) == ("queued", False)


def test_plan_fill_leaves_a_stored_intake_alone():
    assert store_fill.plan_fill([_INTAKE], [_INITIATIVE, _INTAKE]).writes == ()


def test_plan_fill_keeps_a_done_intake_done():
    (write,) = store_fill.plan_fill([{**_INTAKE, "state": "done"}], [_INITIATIVE]).writes
    assert write["state"] == "done"
