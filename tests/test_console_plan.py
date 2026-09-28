from agent_tools.console_plan import confirm_line, plan_command


def test_draft_approve():
    assert plan_command({"kind": "draft", "id": "foo"}, "a") == [
        "cox",
        "route",
        "approve",
        "foo",
    ]


def test_draft_decline():
    assert plan_command({"kind": "draft", "id": "foo"}, "x") == [
        "cox",
        "route",
        "decline",
        "foo",
        "--reason",
        "declined from the console",
    ]


def test_host_drain():
    assert plan_command({"kind": "host", "name": "bar"}, "d") == [
        "cox",
        "host",
        "drain",
        "bar",
    ]


def test_host_activate():
    assert plan_command({"kind": "host", "name": "bar"}, "u") == [
        "cox",
        "host",
        "activate",
        "bar",
    ]


def test_chair_take():
    assert plan_command({"kind": "chair"}, "t") == ["cox", "route", "chair", "take"]


def test_chair_release():
    assert plan_command({"kind": "chair"}, "r") == [
        "cox",
        "route",
        "chair",
        "release",
    ]


def test_lane_stop():
    assert plan_command({"kind": "lane", "run": "run-1", "pid": 123}, "s") == [
        "kill",
        "-TERM",
        "123",
    ]


def test_unknown_key():
    assert plan_command({"kind": "draft", "id": "foo"}, "z") is None


def test_unknown_kind():
    assert plan_command({"kind": "nope"}, "a") is None


def test_confirm_line():
    argv = ["cox", "route", "approve", "foo"]
    assert confirm_line(argv) == "run: cox route approve foo? [y/N]"
