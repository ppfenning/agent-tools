from __future__ import annotations

import dataclasses
import socket
import threading

import pytest

from agent_tools.forgejo_api import (
    ForgejoError,
    ForgejoSettings,
    ForgejoSettingsError,
    MissingTokenError,
    build_headers,
    parse_body,
    read_settings,
    redact,
    request,
)
from tests.fake_forgejo import FakeForgejo

GOOD = {"forgejo_base_url": "http://git.lan:3000/", "forgejo_token_env": "FORGEJO_TOKEN"}
ENV = {"FORGEJO_TOKEN": "s3cr3t-value"}
PULLS = "/api/v1/repos/o/r/pulls"


def test_settings_strip_one_trailing_slash():
    assert read_settings(GOOD) == ForgejoSettings("http://git.lan:3000", "FORGEJO_TOKEN")


def test_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        read_settings(GOOD).base_url = "x"  # type: ignore[misc]


def test_settings_accept_https_bare_ip_and_lowercase_env():
    got = read_settings({"forgejo_base_url": "https://10.0.0.5", "forgejo_token_env": "forgejo_token"})
    assert got == ForgejoSettings("https://10.0.0.5", "forgejo_token")


@pytest.mark.parametrize(
    ("forge", "key"),
    [
        ({"forgejo_token_env": "T"}, "forgejo_base_url"),
        ({"forgejo_base_url": "http://h"}, "forgejo_token_env"),
        ({"forgejo_base_url": 3, "forgejo_token_env": "T"}, "forgejo_base_url"),
        ({"forgejo_base_url": "http://h", "forgejo_token_env": ["T"]}, "forgejo_token_env"),
        ({"forgejo_base_url": "", "forgejo_token_env": "T"}, "forgejo_base_url"),
        ({"forgejo_base_url": "http://h", "forgejo_token_env": "   "}, "forgejo_token_env"),
        ({"forgejo_base_url": "git.lan:3000", "forgejo_token_env": "T"}, "forgejo_base_url"),
        ({"forgejo_base_url": "ftp://git.lan", "forgejo_token_env": "T"}, "forgejo_base_url"),
    ],
)
def test_settings_reject_and_name_the_key(forge, key):
    with pytest.raises(ForgejoSettingsError, match=key):
        read_settings(forge)


def test_redact_replaces_every_occurrence_and_ignores_empty_token():
    assert redact("a tok b tok", "tok") == "a *** b ***"
    assert redact("unchanged", "") == "unchanged"


def test_build_headers():
    assert build_headers("t", False) == {"Authorization": "token t", "Accept": "application/json"}
    assert build_headers("t", True)["Content-Type"] == "application/json"


def test_parse_body_shapes():
    assert parse_body(b"", "t") is None
    assert parse_body(b'{"a": 1}', "t") == {"a": 1}


def test_parse_body_redacts_a_non_json_body():
    assert parse_body(b"bad token s3cr3t-value", "s3cr3t-value") == {"message": "bad token ***"}


@pytest.mark.parametrize("env", [{}, {"FORGEJO_TOKEN": ""}, {"OTHER_TOKEN": "s3cr3t-value"}])
def test_missing_token_names_the_variable_and_no_value(env):
    with pytest.raises(MissingTokenError) as info:
        request(read_settings(GOOD), env, "GET", PULLS)
    err = info.value
    assert "FORGEJO_TOKEN" in str(err)
    assert "s3cr3t-value" not in f"{err!s} {err!r} {err.args}"


def test_transport_error_on_a_closed_port_hides_the_token():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = ForgejoSettings(f"http://127.0.0.1:{port}", "FORGEJO_TOKEN")
    with pytest.raises(ForgejoError) as info:
        request(settings, ENV, "GET", PULLS + "?t=s3cr3t-value", timeout=2)
    err = info.value
    assert "s3cr3t-value" not in f"{err!s} {err!r} {err.args}"
    assert "***" in str(err)


def test_redact_hides_the_repr_escaped_token():
    assert redact("Invalid header value b'token s3cr3t\\n'", "s3cr3t\n") == "Invalid header value b'token ***'"


def test_a_token_with_a_newline_raises_forgejo_error_without_the_token():
    with FakeForgejo() as fake:
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        with pytest.raises(ForgejoError) as info:
            request(settings, {"FORGEJO_TOKEN": "s3cr3t-value\n"}, "GET", PULLS)
    err = info.value
    assert "s3cr3t" not in f"{err!s} {err!r} {err.args}"
    assert "ValueError" in str(err)


def _truncated_reply(status: int) -> str:
    """Serves one response that promises 100 bytes and sends 5, so reading the body fails."""
    server = socket.create_server(("127.0.0.1", 0))

    def answer() -> None:
        conn, _ = server.accept()
        with conn, server:
            conn.recv(65536)
            conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Length: 100\r\n\r\nshort".encode())

    threading.Thread(target=answer, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}"


@pytest.mark.parametrize("status", [200, 500])
def test_a_truncated_body_raises_forgejo_error(status):
    settings = ForgejoSettings(_truncated_reply(status), "FORGEJO_TOKEN")
    with pytest.raises(ForgejoError, match="IncompleteRead"):
        request(settings, ENV, "GET", PULLS, timeout=5)


def test_create_pull():
    with FakeForgejo() as fake:
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        status, body = request(settings, ENV, "POST", PULLS, body={"head": "feat", "base": "main", "title": "t"})
        assert status == 201
        assert body["number"] == 1 and body["head"]["ref"] == "feat" and body["base"]["ref"] == "main"
        assert body["mergeable"] is True and body["html_url"].endswith("/pulls/1")
        assert fake.requests[0] == {
            "method": "POST",
            "path": PULLS,
            "authorization": "token s3cr3t-value",
            "body": {"head": "feat", "base": "main", "title": "t"},
        }


def test_list_and_get_pull():
    with FakeForgejo() as fake:
        fake.add_pull(7, "feat", "main", "abc")
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        listed, one = request(settings, ENV, "GET", PULLS), request(settings, ENV, "GET", PULLS + "/7")
        assert listed[0] == 200 and [p["number"] for p in listed[1]] == [7]
        assert one[0] == 200 and one[1]["head"]["sha"] == "abc"
        assert request(settings, ENV, "GET", PULLS + "/9") == (404, {"message": "not found"})
        assert {r["authorization"] for r in fake.requests} == {"token s3cr3t-value"}


def test_combined_status_defaults_to_pending_then_follows_the_script():
    with FakeForgejo() as fake:
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        path = "/api/v1/repos/o/r/commits/abc/status"
        assert request(settings, ENV, "GET", path) == (200, {"state": "pending", "statuses": []})
        fake.set_status("abc", "success")
        status, body = request(settings, ENV, "GET", path)
        assert (status, body["state"], len(body["statuses"])) == (200, "success", 1)
        assert fake.requests[-1]["authorization"] == "token s3cr3t-value"


def test_set_mergeable_changes_the_pull():
    with FakeForgejo() as fake:
        fake.add_pull(1, "feat", "main", "abc")
        fake.set_mergeable(1, False)
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        assert request(settings, ENV, "GET", PULLS + "/1")[1]["mergeable"] is False


def test_merge_records_the_body_and_can_be_refused():
    with FakeForgejo() as fake:
        fake.add_pull(1, "feat", "main", "abc")
        fake.add_pull(2, "other", "main", "def")
        fake.refuse_merge(2, message="blocked")
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        assert request(settings, ENV, "POST", PULLS + "/1/merge", body={"Do": "squash"}) == (200, None)
        assert request(settings, ENV, "POST", PULLS + "/2/merge", body={"Do": "squash"}) == (
            405,
            {"message": "blocked"},
        )
        assert fake.requests[0]["body"] == {"Do": "squash"}
        assert fake.requests[0]["authorization"] == "token s3cr3t-value"


def test_delete_branch():
    with FakeForgejo() as fake:
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        assert request(settings, ENV, "DELETE", "/api/v1/repos/o/r/branches/feat") == (204, None)
        assert request(settings, ENV, "DELETE", "/api/v1/repos/o/r/branches/feature%2Fx") == (204, None)
        assert fake.deleted_branches == ["feat", "feature/x"]
        assert fake.requests[0]["authorization"] == "token s3cr3t-value"


def test_a_query_string_does_not_change_the_route():
    with FakeForgejo() as fake:
        fake.add_pull(3, "feat", "main", "abc")
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        status, body = request(settings, ENV, "GET", PULLS + "?state=open&head=feat")
        assert (status, [p["number"] for p in body]) == (200, [3])
        assert fake.requests[0]["path"] == PULLS + "?state=open&head=feat"


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/v1/nope"), ("PUT", PULLS), ("PATCH", PULLS)])
def test_unknown_route_or_method_is_a_json_404(method, path):
    with FakeForgejo() as fake:
        settings = ForgejoSettings(fake.base_url, "FORGEJO_TOKEN")
        assert request(settings, ENV, method, path) == (404, {"message": "not found"})
