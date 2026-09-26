from datetime import UTC, datetime

from agent_tools.draft_state import Plan, Refusal, plan_approve, plan_decline

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
STAMP = "2026-09-26T12:00:00+00:00"
BODY = "\n# Title  \nBody: with colon\n\ttabbed\n"
HEAD = "---\ntitle: T\ndraft: {}\nproposed_by: steward\nproposed_at: 2026-09-25T09:00:00+00:00\n"


def initiative(draft: str = "true") -> str:
    return HEAD.format(draft) + "---\nProse.\n"


def ticket(state: str) -> str:
    return f"---\nid: x\nstate: {state}\n---{BODY}"


def test_approve_moves_todo_to_ready_and_clears_draft():
    plan = plan_approve(initiative(), {"a": ticket("todo"), "b": ticket("todo")}, None, "pat", NOW)
    assert plan == Plan(
        initiative_text=HEAD.format("false") + f"approved_by: pat\napproved_at: {STAMP}\n---\nProse.\n",
        tickets={"a": ticket("ready"), "b": ticket("ready")},
        moves=(("a", "todo", "ready"), ("b", "todo", "ready")),
    )


def test_approve_leaves_ready_and_done_tickets_alone():
    tickets = {"a": ticket("todo"), "b": ticket("ready"), "c": ticket("done")}
    plan = plan_approve(initiative(), tickets, None, "pat", NOW)
    assert isinstance(plan, Plan)
    assert plan.tickets == {"a": ticket("ready")}
    assert plan.moves == (("a", "todo", "ready"),)
    assert "draft: false" in plan.initiative_text


def test_approve_with_task_moves_one_ticket_and_keeps_draft_true():
    plan = plan_approve(initiative(), {"a": ticket("todo"), "b": ticket("todo")}, "a", "pat", NOW)
    assert isinstance(plan, Plan)
    assert plan.tickets == {"a": ticket("ready")}
    assert plan.moves == (("a", "todo", "ready"),)
    assert "draft: true\n" in plan.initiative_text
    assert f"approved_by: pat\napproved_at: {STAMP}\n" in plan.initiative_text


def test_approve_of_last_todo_task_clears_draft():
    plan = plan_approve(initiative(), {"a": ticket("ready"), "b": ticket("todo")}, "b", "pat", NOW)
    assert isinstance(plan, Plan)
    assert "draft: false\n" in plan.initiative_text
    assert plan.moves == (("b", "todo", "ready"),)


def test_decline_moves_todo_to_dropped_and_records_reason():
    plan = plan_decline(initiative(), {"a": ticket("todo"), "b": ticket("done")}, 'too big: "split" it', "pat", NOW)
    assert plan == Plan(
        initiative_text=HEAD.format("false")
        + f'declined_by: pat\ndeclined_at: {STAMP}\ndeclined_reason: "too big: \\"split\\" it"\n---\nProse.\n',
        tickets={"a": ticket("dropped")},
        moves=(("a", "todo", "dropped"),),
    )


def test_decline_with_no_reason_refuses():
    assert isinstance(plan_decline(initiative(), {"a": ticket("todo")}, "  ", "pat", NOW), Refusal)


def test_non_draft_initiative_refuses():
    tickets = {"a": ticket("todo")}
    assert isinstance(plan_approve(initiative("false"), tickets, None, "pat", NOW), Refusal)
    assert isinstance(plan_decline(initiative("false"), tickets, "no", "pat", NOW), Refusal)
    assert isinstance(plan_approve("no frontmatter\n", tickets, None, "pat", NOW), Refusal)


def test_unknown_task_id_refuses():
    assert plan_approve(initiative(), {"a": ticket("todo")}, "zz", "pat", NOW) == Refusal("no ticket named zz")


def test_no_todo_ticket_refuses():
    tickets = {"a": ticket("ready")}
    assert isinstance(plan_approve(initiative(), tickets, None, "pat", NOW), Refusal)
    assert isinstance(plan_approve(initiative(), tickets, "a", "pat", NOW), Refusal)
    assert isinstance(plan_decline(initiative(), tickets, "no", "pat", NOW), Refusal)
