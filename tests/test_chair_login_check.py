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
    assert row["versions_json"] == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth"}


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


def test_a_claude_code_runner_call_builds_the_same_argv_and_never_calls_env_check_argv(monkeypatch) -> None:
    calls: list = []

    def ssh_run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, '{"loggedIn": true}'

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    def _boom(_var: str) -> list[str]:
        raise AssertionError("env_check_argv must not be called for the claude-code runner")

    monkeypatch.setattr(chair_login_check, "env_check_argv", _boom)

    row = check_login_on_host("shed", "user@shed", {}, NOW, ssh_run, cli_run, runner="claude-code")

    assert calls == [["ssh", "user@shed", "claude auth status"]]
    assert row["versions_json"] == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth"}


def test_an_env_vars_runner_with_every_variable_present_is_logged_in() -> None:
    def ssh_run(argv: list[str]) -> tuple[int, str]:
        return 0, "some-secret-value\n"

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    row = check_login_on_host(
        "shed", "user@shed", {}, NOW, ssh_run, cli_run, runner="litellm", env_names=("MY_API_KEY",)
    )

    versions = row["versions_json"]
    assert versions["login_ok"] is True
    assert versions["check"] == "env_vars"
    assert versions["login_checked_at"] == NOW
    assert "reason" not in versions


def test_an_env_vars_runner_with_a_missing_variable_names_it_in_the_reason() -> None:
    def ssh_run(argv: list[str]) -> tuple[int, str]:
        return 0, ""

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    row = check_login_on_host(
        "shed", "user@shed", {}, NOW, ssh_run, cli_run, runner="litellm", env_names=("MY_API_KEY",)
    )

    versions = row["versions_json"]
    assert versions["login_ok"] is False
    assert versions["check"] == "env_vars"
    assert "MY_API_KEY" in versions["reason"]


def test_an_env_vars_runner_on_an_unreachable_host_is_not_logged_in() -> None:
    def ssh_run(argv: list[str]) -> tuple[int, str]:
        return 255, "ssh: connect to host x: Connection refused"

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    row = check_login_on_host(
        "shed", "user@shed", {}, NOW, ssh_run, cli_run, runner="litellm", env_names=("MY_API_KEY",)
    )

    assert row["versions_json"]["login_ok"] is False


def test_an_env_vars_runner_with_no_env_names_is_not_logged_in_and_runs_nothing() -> None:
    calls: list = []

    def ssh_run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "unused"

    def cli_run(argv: list[str]) -> dict:
        return {"name": "shed", "versions_json": _versions_arg(argv)}

    row = check_login_on_host("shed", "user@shed", {}, NOW, ssh_run, cli_run, runner="litellm")

    assert row["versions_json"] == {
        "login_ok": False,
        "login_checked_at": NOW,
        "check": "env_vars",
        "reason": "no auth_env or endpoint_env configured",
    }
    assert calls == []


def test_a_failed_check_stores_the_login_url_from_the_ssh_output() -> None:
    def ssh_run(argv: list[str]) -> tuple[int, str]:
        return 255, "# Tailscale SSH requires an additional check.\n# To authenticate, visit: https://login.tailscale.com/a/abc123\n"

    captured: list = []

    def cli_run(argv: list[str]) -> dict:
        captured.append(_versions_arg(argv))
        return {}

    check_login_on_host("shed", "user@shed", {}, NOW, ssh_run, cli_run)

    assert captured[0]["login_ok"] is False
    assert captured[0]["login_url"] == "https://login.tailscale.com/a/abc123"


def test_a_failed_check_with_no_url_stores_no_login_url_key() -> None:
    captured: list = []

    def cli_run(argv: list[str]) -> dict:
        captured.append(_versions_arg(argv))
        return {}

    check_login_on_host("shed", "user@shed", {}, NOW, lambda argv: (1, "connection refused"), cli_run)

    assert "login_url" not in captured[0]


def test_a_passing_check_clears_a_stored_login_url() -> None:
    captured: list = []

    def cli_run(argv: list[str]) -> dict:
        captured.append(_versions_arg(argv))
        return {}

    stored = {"login_ok": False, "login_url": "https://login.tailscale.com/a/abc123"}
    check_login_on_host("shed", "user@shed", stored, NOW, lambda argv: (0, '{"loggedIn": true}'), cli_run)

    assert captured[0] == {"login_ok": True, "login_checked_at": NOW, "check": "claude_auth"}
