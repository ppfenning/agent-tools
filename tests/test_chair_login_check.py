import json

from agent_tools import chair_login_check
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


def test_the_loop_s_own_host_is_checked_locally_with_no_ssh(monkeypatch) -> None:
    monkeypatch.setattr(chair_login_check.socket, "gethostname", lambda: "omarchy")
    calls: list = []

    def ssh_run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, '{"loggedIn": true}'

    def cli_run(argv: list[str]) -> dict:
        return {"name": "omarchy", "versions_json": _versions_arg(argv)}

    row = check_login_on_host("omarchy", "user@omarchy", {}, NOW, ssh_run, cli_run)

    assert calls == [["claude", "auth", "status"]]
    assert row["versions_json"] == {"login_ok": True, "login_checked_at": NOW}


def test_a_different_host_than_the_loop_s_own_is_still_checked_over_ssh(monkeypatch) -> None:
    monkeypatch.setattr(chair_login_check.socket, "gethostname", lambda: "omarchy")
    calls: list = []

    def ssh_run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, '{"loggedIn": true}'

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    check_login_on_host("shed", "user@shed", {}, NOW, ssh_run, cli_run)

    assert calls == [["ssh", "user@shed", "claude auth status"]]
