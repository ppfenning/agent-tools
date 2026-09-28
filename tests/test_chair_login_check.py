import json

from agent_tools.chair_login_check import check_login_on_host

NOW = "2026-09-28T00:00:00+00:00"


def _versions_arg(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--versions") + 1])


def test_a_logged_in_host_merges_login_ok_true_and_now_and_keeps_existing_keys() -> None:
    calls: list = []

    def ssh_run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, '{"loggedIn": true}'

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    row = check_login_on_host("shed", "user@shed", {"cox": "1.2.3"}, NOW, ssh_run, cli_run)

    versions = row["versions_json"]
    assert versions["login_ok"] is True
    assert versions["login_checked_at"] == NOW
    assert versions["cox"] == "1.2.3"
    assert calls == [["ssh", "user@shed", "claude auth status"]]


def test_an_unparseable_ssh_reply_merges_login_ok_false() -> None:
    def ssh_run(argv: list[str]) -> tuple[int, str]:
        return 1, "connection refused"

    captured: list = []

    def cli_run(argv: list[str]) -> dict:
        captured.append(_versions_arg(argv))
        return {}

    check_login_on_host("shed", "user@shed", {}, NOW, ssh_run, cli_run)

    assert captured[0]["login_ok"] is False
