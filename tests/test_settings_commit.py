from __future__ import annotations

import subprocess
from pathlib import Path

from agent_tools import cox_settings

CARTRIDGE = "policy:\n  dispatch:\n    max_in_flight: 2\n"
KEY = "policy.dispatch.max_in_flight"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    """A profile naming a committed cartridge in its own git repo; the profile file sits outside the repo."""
    cartridge_dir = tmp_path / "cartridges" / "pat"
    cartridge_dir.mkdir(parents=True)
    cartridge = cartridge_dir / "cartridge.yaml"
    cartridge.write_text(CARTRIDGE, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: pat\ncartridges_dir: " + str(tmp_path / "cartridges") + "\n", encoding="utf-8")
    _git(cartridge_dir, "init", "-q")
    _git(cartridge_dir, "config", "user.name", "Casey Caller")
    _git(cartridge_dir, "config", "user.email", "casey@example.com")
    _git(cartridge_dir, "add", "cartridge.yaml")
    _git(cartridge_dir, "commit", "-q", "-m", "initial")
    return profile, cartridge


def test_commit_message_uses_the_key_and_raw_value():
    assert cox_settings.commit_message("policy.dispatch.max_in_flight", "4") == "settings: policy.dispatch.max_in_flight = 4"
    assert cox_settings.commit_message("models.build.model", "claude opus") == "settings: models.build.model = claude opus"


def test_commit_command_quotes_the_message():
    assert cox_settings.commit_command("cartridge.yaml", "settings: k = a b") == (
        "git commit -m 'settings: k = a b' -- cartridge.yaml"
    )


def test_commit_on_a_cartridge_key_makes_exactly_one_commit(tmp_path):
    profile, cartridge = _workspace(tmp_path)
    code = cox_settings.run_set(profile, "cartridge", KEY, "4", dry_run=False, commit=True)
    assert code == 0
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "2"
    assert _git(cartridge.parent, "log", "-1", "--format=%s") == "settings: policy.dispatch.max_in_flight = 4"
    assert _git(cartridge.parent, "status", "--porcelain") == ""


def test_without_commit_a_tracked_cartridge_is_written_and_not_committed(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path)
    code = cox_settings.run_set(profile, "cartridge", KEY, "4", dry_run=False)
    assert code == 0
    assert "max_in_flight: 4" in cartridge.read_text(encoding="utf-8")
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "1"
    assert "no commit was made" in capsys.readouterr().out


def test_commit_on_a_profile_key_writes_and_makes_no_commit(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "profile.yaml")
    _git(tmp_path, "-c", "user.name=Casey", "-c", "user.email=c@example.com", "commit", "-q", "-m", "profile")
    code = cox_settings.run_set(profile, "profile", "chair.stale_days", "7", dry_run=False, commit=True)
    assert code == 0
    assert "stale_days" in profile.read_text(encoding="utf-8")
    assert _git(tmp_path, "rev-list", "--count", "HEAD") == "1"
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "1"
    assert "no commit was made" in capsys.readouterr().out


def test_dry_run_with_commit_prints_the_command_and_changes_nothing(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path)
    code = cox_settings.run_set(profile, "cartridge", KEY, "4", dry_run=True, commit=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "+    max_in_flight: 4" in out
    assert "git commit -m 'settings: policy.dispatch.max_in_flight = 4' -- cartridge.yaml" in out
    assert cartridge.read_text(encoding="utf-8") == CARTRIDGE
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "1"
    assert _git(cartridge.parent, "status", "--porcelain") == ""
