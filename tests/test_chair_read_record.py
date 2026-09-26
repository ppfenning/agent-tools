from agent_tools.chair_read_record import ACTION_LOG, action_line, record_action_argv, recorder


def test_action_line_sorts_keys_and_adds_ts_and_epoch():
    action = {"kind": "needs_chair", "initiative": "x", "cause": "code"}
    assert (
        action_line(action, 3, "t")
        == '{"cause": "code", "epoch": 3, "initiative": "x", "kind": "needs_chair", "ts": "t"}'
    )


def test_recorder_appends_lines_in_order(tmp_path):
    record = recorder(tmp_path, lambda: 1, lambda: "t")
    first = {"kind": "needs_chair", "initiative": "a", "cause": "code"}
    second = {"kind": "needs_chair", "initiative": "b", "cause": "spec"}
    record(first)
    record(second)
    assert (tmp_path / ACTION_LOG).read_text().splitlines() == [
        action_line(first, 1, "t"),
        action_line(second, 1, "t"),
    ]


ACTION = {"kind": "needs_chair", "initiative": "a", "cause": "code"}


def test_recorder_hands_the_appended_line_to_the_store(tmp_path):
    calls = []
    record = recorder(tmp_path, lambda: 1, lambda: "t", store=lambda argv: calls.append(argv) or (0, ""), holder="h")
    record(ACTION)
    line = action_line(ACTION, 1, "t")
    assert (tmp_path / ACTION_LOG).read_text() == line + "\n"
    assert calls == [["record-action", "--holder", "h", line]]


def test_a_failed_store_write_keeps_the_line_and_prints_one_stderr_line(tmp_path, capsys):
    recorder(tmp_path, lambda: 1, lambda: "t", store=lambda argv: (2, "bad"), holder="h")(ACTION)
    assert (tmp_path / ACTION_LOG).read_text() == action_line(ACTION, 1, "t") + "\n"
    assert capsys.readouterr().err == "chair: action not recorded in the store: bad\n"


def test_with_no_store_only_the_append_happens(tmp_path, capsys):
    recorder(tmp_path, lambda: 1, lambda: "t")(ACTION)
    assert (tmp_path / ACTION_LOG).read_text() == action_line(ACTION, 1, "t") + "\n"
    assert capsys.readouterr().err == ""


def test_the_record_action_arguments_follow_the_module():
    assert record_action_argv("h", "{}") == ["record-action", "--holder", "h", "{}"]
