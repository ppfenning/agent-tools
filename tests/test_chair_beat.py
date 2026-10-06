from __future__ import annotations

import threading
import time
from collections.abc import Callable

from agent_tools.chair_beat import beat_in_thread

INTERVAL = 0.01


def _wait_until(cond: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_beats_continue_while_main_thread_blocks() -> None:
    calls: list[int] = []
    stop = threading.Event()
    thread = beat_in_thread(lambda: calls.append(1), INTERVAL, stop, lambda exc: None)
    try:
        threading.Event().wait(0.2)
        assert len(calls) >= 3
        assert thread.daemon is True
    finally:
        stop.set()
        thread.join(timeout=1)


def test_stop_ends_thread_and_count_stops_rising() -> None:
    calls: list[int] = []
    stop = threading.Event()
    thread = beat_in_thread(lambda: calls.append(1), INTERVAL, stop, lambda exc: None)
    try:
        assert _wait_until(lambda: len(calls) >= 1)
    finally:
        stop.set()
        thread.join(timeout=1)
    assert not thread.is_alive()
    settled = len(calls)
    threading.Event().wait(INTERVAL * 5)
    assert len(calls) == settled


def test_stop_is_prompt_with_a_long_interval() -> None:
    stop = threading.Event()
    thread = beat_in_thread(lambda: None, 30.0, stop, lambda exc: None)
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_raising_beat_is_recorded_and_later_beats_still_run() -> None:
    calls: list[int] = []
    errors: list[Exception] = []

    def beat() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("first beat fails")

    stop = threading.Event()
    thread = beat_in_thread(beat, INTERVAL, stop, errors.append)
    try:
        assert _wait_until(lambda: len(calls) >= 3)
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=1)
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
