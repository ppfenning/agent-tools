from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_tools.notify_core import KINDS, Event, NotifyConfig
from agent_tools.notify_dispatch import dispatch

NOW = 1_000_000.0


class _Fake:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200


@pytest.fixture
def fake() -> Iterator[tuple[_Fake, NotifyConfig]]:
    state = _Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            state.requests.append({"headers": dict(self.headers.items()), "body": self.rfile.read(length)})
            self.send_response(state.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, NotifyConfig(f"http://127.0.0.1:{server.server_address[1]}/topic")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _event(kind: str = "stall", key: str = "k1") -> Event:
    return Event(kind, key, f"{kind} title", f"{kind} body", f"https://example.com/{kind}")


def test_each_of_the_five_kinds_arrives_once_with_title_body_and_click(
    fake: tuple[_Fake, NotifyConfig], tmp_path: Path
) -> None:
    state, config = fake
    events = [_event(kind, f"key-{kind}") for kind in KINDS]
    assert dispatch(events, config, tmp_path / "sent.json", NOW) == [f"key-{k}" for k in KINDS]
    got = [(r["headers"]["Title"], r["body"], r["headers"]["Click"]) for r in state.requests]
    assert got == [(f"{k} title", f"{k} body".encode(), f"https://example.com/{k}") for k in KINDS]


def test_same_key_twice_within_an_hour_arrives_once_and_again_after(
    fake: tuple[_Fake, NotifyConfig], tmp_path: Path
) -> None:
    state, config = fake
    path = tmp_path / "sent.json"
    assert dispatch([_event()], config, path, NOW) == ["k1"]
    assert dispatch([_event()], config, path, NOW + 3599) == []
    assert len(state.requests) == 1
    assert dispatch([_event()], config, path, NOW + 3600) == ["k1"]
    assert len(state.requests) == 2


def test_different_keys_of_one_kind_both_arrive(fake: tuple[_Fake, NotifyConfig], tmp_path: Path) -> None:
    state, config = fake
    assert dispatch([_event(key="a"), _event(key="b")], config, tmp_path / "sent.json", NOW) == ["a", "b"]
    assert len(state.requests) == 2


def test_none_config_sends_nothing_and_creates_no_state_file(fake: tuple[_Fake, NotifyConfig], tmp_path: Path) -> None:
    state, _ = fake
    path = tmp_path / "sub" / "sent.json"
    assert dispatch([_event()], None, path, NOW) == []
    assert state.requests == []
    assert not path.exists()
    assert not path.parent.exists()


def test_failing_endpoint_leaves_key_unrecorded_so_next_call_sends_it(
    fake: tuple[_Fake, NotifyConfig], tmp_path: Path
) -> None:
    state, config = fake
    path = tmp_path / "sent.json"
    state.status = 500
    assert dispatch([_event()], config, path, NOW) == []
    assert not path.exists()
    state.status = 200
    assert dispatch([_event()], config, path, NOW + 1) == ["k1"]
    assert json.loads(path.read_text()) == {"k1": NOW + 1}


def test_corrupt_state_file_is_treated_as_empty(fake: tuple[_Fake, NotifyConfig], tmp_path: Path) -> None:
    state, config = fake
    path = tmp_path / "sent.json"
    path.write_text("{not json")
    assert dispatch([_event()], config, path, NOW) == ["k1"]
    assert len(state.requests) == 1
    assert json.loads(path.read_text()) == {"k1": NOW}
    assert list(tmp_path.glob("*.tmp")) == []


def test_post_parameter_is_used_and_unchanged_state_is_not_rewritten(tmp_path: Path) -> None:
    calls: list[tuple] = []

    def post(*args: object) -> bool:
        calls.append(args)
        return True

    path = tmp_path / "deep" / "sent.json"
    config = NotifyConfig("http://x/topic")
    assert dispatch([_event()], config, path, NOW, post=post) == ["k1"]
    assert calls == [("http://x/topic", "stall title", "stall body", "https://example.com/stall")]
    before = path.stat().st_ino
    assert dispatch([_event()], config, path, NOW + 1, post=post) == []
    assert len(calls) == 1
    assert path.stat().st_ino == before


def test_failed_push_of_an_expired_key_is_not_recorded(fake: tuple[_Fake, NotifyConfig], tmp_path: Path) -> None:
    state, config = fake
    path = tmp_path / "sent.json"
    assert dispatch([_event()], config, path, NOW) == ["k1"]
    state.status = 500
    assert dispatch([_event()], config, path, NOW + 3600) == []
    assert json.loads(path.read_text()) == {}
    state.status = 200
    assert dispatch([_event()], config, path, NOW + 3601) == ["k1"]
    assert len(state.requests) == 3


def test_raising_post_keeps_earlier_pushes_recorded(tmp_path: Path) -> None:
    def post(url: str, title: str, body: str, click: str = "") -> bool:
        if title.startswith("host_login"):
            raise RuntimeError("boom")
        return True

    path = tmp_path / "sent.json"
    events = [_event("stall", "a"), _event("host_login", "b")]
    assert dispatch(events, NotifyConfig("http://x/topic"), path, NOW, post=post) == ["a"]
    assert json.loads(path.read_text()) == {"a": NOW}
