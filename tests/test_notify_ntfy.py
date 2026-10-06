from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_tools.notify_ntfy import _header_value, post


class _Fake:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200


@pytest.fixture
def fake() -> Iterator[tuple[_Fake, str]]:
    state = _Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            state.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(state.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}/topic"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_body_and_both_headers_arrive(fake: tuple[_Fake, str]) -> None:
    state, url = fake
    assert post(url, "Needs you", "run 7 is blocked", click="https://example.com/r/7")
    (req,) = state.requests
    assert req["path"] == "/topic"
    assert req["body"] == b"run 7 is blocked"
    assert req["headers"]["Title"] == "Needs you"
    assert req["headers"]["Click"] == "https://example.com/r/7"


def test_no_click_header_when_click_empty(fake: tuple[_Fake, str]) -> None:
    state, url = fake
    assert post(url, "t", "b")
    (req,) = state.requests
    assert "Click" not in req["headers"]


def test_500_gives_false(fake: tuple[_Fake, str]) -> None:
    state, url = fake
    state.status = 500
    assert post(url, "t", "b") is False
    assert len(state.requests) == 1


def test_closed_port_gives_false() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert post(f"http://127.0.0.1:{port}/topic", "t", "b", timeout=1.0) is False


def test_non_ascii_title_does_not_raise(fake: tuple[_Fake, str]) -> None:
    state, url = fake
    assert post(url, "Café ✓", "b", click="https://example.com/é") is True
    (req,) = state.requests
    assert str(make_header(decode_header(req["headers"]["Title"]))) == "Café ✓"


def test_header_value_ascii_passthrough() -> None:
    assert _header_value("Needs you") == "Needs you"
    assert _header_value("é") == "=?UTF-8?B?w6k=?="
