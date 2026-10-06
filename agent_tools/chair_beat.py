"""Beat a lease from a daemon thread so a long tick cannot let it go stale."""

from __future__ import annotations

import threading
from collections.abc import Callable


def _run(
    beat: Callable[[], None],
    interval: float,
    stop: threading.Event,
    on_error: Callable[[Exception], None],
) -> None:
    while not stop.wait(interval):
        try:
            beat()
        except Exception as exc:
            on_error(exc)


def beat_in_thread(
    beat: Callable[[], None],
    interval: float,
    stop: threading.Event,
    on_error: Callable[[Exception], None],
) -> threading.Thread:
    """Start a daemon thread calling `beat` every `interval` seconds until `stop` is set; a raising beat goes to `on_error` and never ends the thread."""
    thread = threading.Thread(target=_run, args=(beat, interval, stop, on_error), daemon=True)
    thread.start()
    return thread
