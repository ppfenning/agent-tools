import dataclasses

from agent_tools.home_model import (
    Drill,
    Facts,
    Intake,
    Land,
    Quit,
    Refuse,
    Send,
    Setup,
    Span,
    State,
    Talk,
    attention_pane,
    backlog_pane,
    chair_pane,
    chat_pane,
    frame,
    health_pane,
    layout,
    leader_pane,
    runs_pane,
    step,
    window_pane,
)
from agent_tools.runs_top import Row, render

_ROW = Row(run="r1", alive=True, phase="build", node="build-in-worktree", attempt=1, turns=3, cost_usd=1.5, verdict="", status="running", ceiling="")

_WINDOW = {"tier": "sonnet", "effort_ceiling": "high", "spent_usd": 12.5, "time_to_reset": "2h15m", "block_left": 0.52}
_BACKLOG = {"queued": 4, "decomposed": 2, "landed": 9, "ready": {"tools-home": 3}}
_RUNS_PANE = tuple((Span(line),) for line in render([_ROW], 80))


def _facts(**over) -> Facts:
    base = {"leader": None, "leader_liveness": "none", "runs_rows": (_ROW,), "backlog": _BACKLOG, "window": _WINDOW, "now": 0.0}
    return Facts(**{**base, **over})


def _live_leader_facts(**over) -> Facts:
    base = {"leader": {"session": "s1", "heartbeat_at": "1970-01-01T00:00:10+00:00"}, "leader_liveness": "live", "now": 70.0}
    return _facts(**{**base, **over})


def test_leader_pane_marks_attention_with_the_alert_role_when_stale_and_a_run_is_alive():
    lines = chair_pane(_facts(leader={"session": "s1"}, leader_liveness="stale"), 80)
    assert lines[0][0].role == "alert"


def test_leader_pane_marks_attention_when_leader_is_crashed_and_a_run_is_alive():
    lines = chair_pane(_facts(leader={"session": "s1"}, leader_liveness="crashed"), 80)
    assert lines[0][0].role == "alert"


def test_leader_pane_is_plain_with_no_heartbeat_when_the_leader_carries_none():
    lines = chair_pane(_facts(leader={"session": "s1"}, leader_liveness="live"), 80)
    assert lines == ((Span("holder: s1  status: live  heartbeat: n/a", "ok"),),)


def test_leader_pane_shows_heartbeat_age_from_facts_now():
    lines = chair_pane(_live_leader_facts(), 80)
    assert lines == ((Span("holder: s1  status: live  heartbeat: 60s ago", "ok"),),)


def test_facts_chair_and_chair_liveness_mirror_the_stored_leader_fields():
    facts = _facts(leader={"session": "s1"}, leader_liveness="live")
    assert facts.chair == {"session": "s1"}
    assert facts.chair_liveness == "live"


def test_state_chair_liveness_mirrors_the_stored_leader_liveness():
    state = State(plugin_dir="/p", leader_liveness="live", other_holder=None)
    assert state.chair_liveness == "live"


def test_leader_pane_is_a_back_compat_alias_for_chair_pane():
    assert leader_pane is chair_pane


def test_runs_pane_returns_runs_top_render_unchanged():
    assert runs_pane(_facts(), 80) == _RUNS_PANE


def test_attention_pane_shows_one_line_per_stop_reason_and_skips_running():
    rows = (
        dataclasses.replace(_ROW, run="r1", status="exited"),
        dataclasses.replace(_ROW, run="r2", status="quarantined"),
        dataclasses.replace(_ROW, run="r3", status="budget"),
        dataclasses.replace(_ROW, run="r4", status="running"),
    )
    assert attention_pane(_facts(runs_rows=rows), 80) == (
        "r1: gate [l]",
        "r2: quarantine [i]",
        "r3: budget stop [i]",
    )


def test_backlog_pane_shows_counts_and_ready_per_initiative():
    assert backlog_pane(_facts(), 80) == (
        (Span("queued 4  decomposed 2  landed 9"),),
        (Span("ready: tools-home=3"),),
    )


def test_window_pane_shows_the_pacing_verdict():
    assert window_pane(_facts(), 80) == (
        (Span("tier sonnet effort high"),),
        (Span("block 52% left  resets in 2h15m"),),
        (Span("spent $12.50  no ceiling set"),),
    )


def test_window_pane_cuts_a_too_long_reason_with_an_ellipsis():
    lines = window_pane(_facts(window={**_WINDOW, "reason": "y" * 100}), 80)
    assert lines == (
        (Span("tier sonnet effort high"),),
        (Span("block 52% left  resets in 2h15m"),),
        (Span("spent $12.50  no ceiling set"),),
        (Span("y" * 79 + "…"),),
    )


def test_window_pane_shows_the_share_left_against_a_set_ceiling():
    window = {**_WINDOW, "ceiling_usd": 60.0, "ceiling_left": 0.1}
    lines = window_pane(_facts(window=window), 80)
    assert lines[2] == (Span("spent $12.50 of $60 ceiling  10% left"),)


def test_window_pane_reads_zero_percent_left_when_spend_is_over_the_ceiling():
    window = {**_WINDOW, "spent_usd": 108.15, "ceiling_usd": 60.0, "ceiling_left": 0.0}
    lines = window_pane(_facts(window=window), 80)
    assert lines[2] == (Span("spent $108.15 of $60 ceiling  0% left"),)


def test_window_pane_shows_a_meter_window_as_percent_of_plan_limit_not_dollars():
    window = {**_WINDOW, "spent_usd": 16.0, "ceiling_usd": 100.0, "ceiling_left": 0.84, "source": "meter"}
    lines = window_pane(_facts(window=window), 80)
    assert lines[2] == (Span("used 16% of plan limit  84% left"),)


def test_step_t_returns_talk_and_only_talk():
    state = State(plugin_dir="/plugins/coxswain", leader_liveness="none", other_holder=None)
    _, effect = step(state, "t")
    assert effect == Talk("/plugins/coxswain")


def test_step_s_returns_setup_and_only_setup():
    state = State(plugin_dir="/plugins/coxswain", leader_liveness="none", other_holder=None)
    _, effect = step(state, "s")
    assert effect == Setup()


def test_step_q_returns_quit_and_only_quit():
    state = State(plugin_dir="/plugins/coxswain", leader_liveness="none", other_holder=None)
    _, effect = step(state, "q")
    assert effect == Quit()


def test_step_t_opens_the_conversation_even_while_another_session_holds_a_live_leader():
    state = State(plugin_dir="/plugins/coxswain", leader_liveness="live", other_holder="s2")
    _, effect = step(state, "t")
    assert effect == Talk("/plugins/coxswain")


def test_step_enter_drills_into_the_selected_run():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None, selected_run="r1")
    _, effect = step(state, "ENTER")
    assert effect == Drill("r1")


def test_step_l_then_l_lands_dry_run_then_apply_then_resets_on_reselection():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None, selected_run="r1", selected_status="exited")
    state, first = step(state, "l")
    assert first == Land("r1", False)
    assert state.land_armed == "r1"

    state, second = step(state, "l")
    assert second == Land("r1", True)
    assert state.land_armed is None

    reselected = State(
        plugin_dir="/p", leader_liveness="none", other_holder=None,
        selected_run="r2", selected_status="exited", land_armed="r1",
    )
    _, third = step(reselected, "l")
    assert third == Land("r2", False)


def test_step_l_is_a_no_op_when_the_selected_run_has_not_exited():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None, selected_run="r1", selected_status="running")
    _, effect = step(state, "l")
    assert effect is None


def test_step_l_resets_the_arm_when_selection_changes_without_an_intervening_l_press():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None, selected_run="r1", selected_status="exited")
    state, _ = step(state, "l")
    assert state.land_armed == "r1"

    visited = dataclasses.replace(state, selected_run="r2", selected_status="running")
    visited, _ = step(visited, "i")
    assert visited.land_armed is None

    returned = dataclasses.replace(visited, selected_run="r1", selected_status="exited")
    _, effect = step(returned, "l")
    assert effect == Land("r1", False)


def test_step_i_returns_intake():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None)
    _, effect = step(state, "i")
    assert effect == Intake()


def test_only_the_landing_key_refuses_under_a_foreign_live_leader():
    state = State(plugin_dir="/p", leader_liveness="live", other_holder="s2", selected_run="r1", selected_status="exited")
    assert step(state, "l")[1] == Refuse("s2")
    assert step(state, "ENTER")[1] == Drill("r1")


def test_step_c_focuses_the_chat_panel_with_no_effect():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None)
    state, effect = step(state, "c")
    assert state.chat_focused is True
    assert effect is None


def test_step_l_while_chat_is_focused_extends_the_draft_and_does_not_land():
    state = State(
        plugin_dir="/p", leader_liveness="none", other_holder=None,
        selected_run="r1", selected_status="exited", chat_focused=True,
    )
    state, effect = step(state, "l")
    assert state.chat_draft == "l"
    assert effect is None
    assert state.land_armed is None


def test_step_enter_while_chat_is_focused_sends_and_clears_the_draft():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None, chat_focused=True, chat_draft="hello")
    state, effect = step(state, "ENTER")
    assert effect == Send("hello")
    assert state.chat_draft == ""


def test_step_esc_while_chat_is_focused_drops_focus_without_sending():
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None, chat_focused=True, chat_draft="hello")
    state, effect = step(state, "ESC")
    assert state.chat_focused is False
    assert effect is None
    assert state.chat_draft == "hello"


def test_chat_pane_renders_sender_lines_and_the_draft():
    thread = ({"from": "operator", "text": "hi"}, {"from": "cos1", "text": "hey"})
    assert chat_pane(thread, 80, "draf") == ("CHAT", "operator: hi", "cos1: hey", "> draf")


def test_chat_pane_cuts_a_too_long_message_with_an_ellipsis():
    thread = ({"from": "operator", "text": "y" * 100},)
    lines = chat_pane(thread, 80, "")
    assert lines[1] == "operator: " + "y" * 69 + "…"


def test_frame_at_a_wide_width_puts_leader_backlog_and_window_boxes_on_one_row():
    facts = _live_leader_facts()
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None)
    top = frame(facts, state, 200, 10)[0]
    assert [s.text for s in top if s.role == "title"] == ["Leader", "Backlog", "Window"]


def test_frame_under_the_side_by_side_width_stacks_the_four_boxes():
    facts = _live_leader_facts()
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None)
    lines = frame(facts, state, 80, 20)
    titles = [s.text for line in lines for s in line if s.role == "title"]
    assert titles == ["Leader", "Backlog", "Window", "Runs"]


def test_frame_returns_every_line_exactly_width_columns():
    facts = _live_leader_facts()
    state = State(plugin_dir="/p", leader_liveness="none", other_holder=None)
    lines = frame(facts, state, 161, 10)
    assert all(sum(len(s.text) for s in line) == 161 for line in lines)


_STATE = State(plugin_dir="/p", leader_liveness="none", other_holder=None)


def _titles(lines) -> list[str]:
    return [s.text for line in lines for s in line if s.role == "title"]


def test_frame_puts_the_regatta_panel_above_the_leader_row_at_a_normal_height():
    assert [t for t in _titles(frame(_live_leader_facts(), _STATE, 200, 40)) if t != "finish"][:2] == ["REGATTA", "Leader"]


def test_frame_omits_the_regatta_panel_below_the_minimum_height():
    assert "REGATTA" not in _titles(frame(_live_leader_facts(), _STATE, 200, 23))


def test_frame_passes_the_tick_to_the_lanes_unchanged():
    lane = frame(_live_leader_facts(tick=7), _STATE, 80, 60)[2]
    assert "<~~<" in "".join(s.text for s in lane)


def test_health_pane_lists_only_the_failing_check_with_its_detail():
    rows = [{"check": "profile", "ok": False, "detail": "missing"}, {"check": "venv", "ok": True, "detail": ""}]
    assert health_pane(rows, 80) == ("profile: missing",)


def test_health_pane_collapses_to_one_line_when_every_row_passes():
    rows = [{"check": "profile", "ok": True, "detail": ""}, {"check": "venv", "ok": True, "detail": ""}]
    assert health_pane(rows, 80) == ("health: ok",)


_TIGHT_PANELS = {
    "chair": ("c1", "c2"),
    "backlog": ("b1", "b2", "b3", "b4"),
    "window": ("w1",),
    "runs": ("r1",),
}


def test_layout_stacks_panels_full_width_under_160_columns():
    assert layout(80, 100, _TIGHT_PANELS) == (
        ("chair", 2),
        ("backlog", 4),
        ("window", 1),
        ("runs", 1),
    )


def test_layout_places_two_columns_at_160_columns():
    assert layout(160, 4, _TIGHT_PANELS) == (
        ("chair", 2),
        ("backlog", 4),
        ("! window", 0),
        ("! runs", 0),
    )


def test_layout_places_two_columns_above_160_columns():
    assert layout(200, 4, _TIGHT_PANELS) == (
        ("chair", 2),
        ("backlog", 4),
        ("! window", 0),
        ("! runs", 0),
    )


def test_layout_cuts_a_panel_that_exceeds_its_height_with_a_marked_last_line():
    panels = {"chair": ("c1",), "backlog": ("b1",), "window": ("w1",), "runs": ("r1", "r2", "r3", "r4", "r5")}
    assert layout(80, 4, panels) == (
        ("chair", 1),
        ("backlog", 1),
        ("window", 1),
        ("! runs", 1),
    )
