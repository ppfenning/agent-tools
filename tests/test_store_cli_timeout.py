import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_tools import store_cli


@pytest.fixture(autouse=True)
def harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_cli, "_harness_python", lambda: Path("/h/python"))


def test_timeout_is_not_available_naming_the_command() -> None:
    def hang(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 60)

    result = store_cli.pause("r1", run=hang, store_url="sqlite:///x.db")

    assert isinstance(result, store_cli.NotAvailable)
    assert "/h/python -m harness.store_cli pause r1" in result.reason
    assert "60 s" in result.reason


def test_run_passes_the_sixty_second_bound() -> None:
    seen: dict[str, Any] = {}

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    store_cli.resume("r1", run=fake, store_url="sqlite:///x.db")

    assert store_cli.STORE_TIMEOUT_S == 60
    assert seen["timeout"] == 60


def test_default_subprocess_run_gets_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake)
    store_cli.runner(Path("/runs"))(["lease", "release", "n", "h", "1"])

    assert seen["timeout"] == 60
