from pathlib import Path

from agent_tools.cli import _dash_dirs


def test_flags_win_over_the_profile_workspace() -> None:
    assert _dash_dirs("r", "w", {"workspace_dir": "/ws"}) == (Path("r"), Path("w"))


def test_the_profile_workspace_is_used_when_no_flag_is_given() -> None:
    assert _dash_dirs(None, None, {"workspace_dir": "/ws"}) == (Path("/ws/runs"), Path("/ws"))


def test_no_profile_falls_back_to_the_current_directory() -> None:
    assert _dash_dirs(None, None, {}) == (Path("runs"), Path("."))
