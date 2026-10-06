from __future__ import annotations

from pathlib import Path

from agent_tools import cox_settings

CARTRIDGE = "policy:\n  dispatch:\n    max_in_flight: 2\n"


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    cartridge = tmp_path / "cartridges" / "pat" / "cartridge.yaml"
    cartridge.parent.mkdir(parents=True)
    cartridge.write_text(CARTRIDGE, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(_profile(tmp_path, "3.0"), encoding="utf-8")
    return profile, cartridge


def _profile(tmp_path: Path, stale_days: str) -> str:
    return (
        f"team: pat\ncartridges_dir: {tmp_path / 'cartridges'}\n"
        "skills_roots: [a, b]\n"
        'sources: {"gh": {"repo": "o/r"}}\n'
        'repo_map: {"r": "/x"}\n'
        f"chair:\n  stale_days: {stale_days}\n"
        "lane_hosts:\n  - name: web\n    capacity: 2\n"
    )


def test_a_valid_profile_set_writes_the_chair_format(tmp_path):
    profile, _ = _workspace(tmp_path)
    code = cox_settings.run_set(profile, "profile", "chair.stale_days", "5", dry_run=False)
    assert code == 0
    assert profile.read_text(encoding="utf-8") == _profile(tmp_path, "5.0")


def test_a_valid_set_writes_the_file(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path)
    code = cox_settings.run_set(profile, "cartridge", "policy.dispatch.max_in_flight", "4", dry_run=False)
    assert code == 0
    assert cartridge.read_text(encoding="utf-8") == "policy:\n  dispatch:\n    max_in_flight: 4\n"
    assert "wrote" in capsys.readouterr().out


def test_a_value_the_chair_cartridge_reader_rejects_is_refused_and_the_file_is_untouched(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path)
    before = cartridge.read_bytes()
    code = cox_settings.run_set(profile, "cartridge", "policy.dispatch.max_in_flight", "0", dry_run=False)
    assert code == 1
    assert capsys.readouterr().out == (
        "error: policy.dispatch.max_in_flight: the chair ignores this value, expected a positive integer\n"
    )
    assert cartridge.read_bytes() == before


def test_a_value_the_chair_profile_parser_rejects_is_refused_with_its_message(tmp_path, capsys):
    profile, _ = _workspace(tmp_path)
    before = profile.read_bytes()
    code = cox_settings.run_set(profile, "profile", "window_ceiling_usd", "abc", dry_run=False)
    assert code == 1
    assert capsys.readouterr().out == "error: line 12:   window_ceiling_usd: abc\n"
    assert profile.read_bytes() == before
