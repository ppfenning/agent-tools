from __future__ import annotations

import subprocess
from pathlib import Path

from agent_tools import cox_settings
from agent_tools.settings_model import SettingRow

CARTRIDGE = "policy:\n  dispatch:\n    max_in_flight: 2\n  review_tier: opus\n"

GROUPED = {
    "lanes and machines": [
        SettingRow("lanes and machines", "cartridge", "policy.dispatch.max_in_flight", 4, "cartridge.yaml", True),
    ],
    "spend and pacing": [],
    "models and tiers": [
        SettingRow("models and tiers", "cartridge", "policy.review_tier", "opus", "cartridge.yaml", False),
    ],
    "profile files": [
        SettingRow("profile files", "profile", "team", "pat", "profile.yaml", False),
    ],
}


def test_json_shape():
    assert cox_settings.to_json(GROUPED) == {
        "sections": [
            {"name": "lanes and machines", "rows": [{
                "section": "lanes and machines", "scope": "cartridge", "key": "policy.dispatch.max_in_flight",
                "value": 4, "source_file": "cartridge.yaml", "tracked": True, "pat_only": False,
            }]},
            {"name": "spend and pacing", "rows": []},
            {"name": "models and tiers", "rows": [{
                "section": "models and tiers", "scope": "cartridge", "key": "policy.review_tier",
                "value": "opus", "source_file": "cartridge.yaml", "tracked": False, "pat_only": True,
            }]},
            {"name": "profile files", "rows": [{
                "section": "profile files", "scope": "profile", "key": "team",
                "value": "pat", "source_file": "profile.yaml", "tracked": False, "pat_only": False,
            }]},
        ]
    }


def test_text_rendering_marks_pat_only_rows():
    assert cox_settings.render_text(GROUPED) == (
        "lanes and machines\n"
        "  cartridge:policy.dispatch.max_in_flight = 4  (cartridge.yaml, tracked)\n"
        "\n"
        "models and tiers\n"
        '  cartridge:policy.review_tier = "opus"  (cartridge.yaml, local)  [pat-only]\n'
        "\n"
        "profile files\n"
        '  profile:team = "pat"  (profile.yaml, local)'
    )


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _workspace(tmp_path: Path, track: bool) -> tuple[Path, Path]:
    """A profile naming `<tmp>/cartridges/pat/cartridge.yaml`, which is committed in a repo when `track`."""
    cartridge_dir = tmp_path / "cartridges" / "pat"
    cartridge_dir.mkdir(parents=True)
    cartridge = cartridge_dir / "cartridge.yaml"
    cartridge.write_text(CARTRIDGE, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: pat\ncartridges_dir: " + str(tmp_path / "cartridges") + "\n", encoding="utf-8")
    if track:
        _git(cartridge_dir, "init", "-q")
        _git(cartridge_dir, "config", "user.name", "Casey Caller")
        _git(cartridge_dir, "config", "user.email", "casey@example.com")
        _git(cartridge_dir, "add", "cartridge.yaml")
        _git(cartridge_dir, "commit", "-q", "-m", "initial")
    return profile, cartridge


def test_commit_message_names_the_caller():
    assert cox_settings.commit_message("cartridge", "policy.dispatch.max_in_flight", "4", "Casey Caller") == (
        "settings: set cartridge:policy.dispatch.max_in_flight = 4 (by Casey Caller)"
    )


def test_target_path_by_scope():
    profile = {"team": "pat", "cartridges_dir": "/c"}
    assert cox_settings.target_path("profile", Path("/p/profile.yaml"), profile) == Path("/p/profile.yaml")
    assert cox_settings.target_path("cartridge", Path("/p/profile.yaml"), profile) == Path("/c/pat/cartridge.yaml")
    assert cox_settings.target_path("cartridge", Path("/p/profile.yaml"), {}) is None
    assert cox_settings.target_path("nope", Path("/p/profile.yaml"), profile) is None


def test_dry_run_prints_the_diff_and_writes_nothing(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path, track=True)
    code = cox_settings.run_set(profile, "cartridge", "policy.dispatch.max_in_flight", "4", dry_run=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "-    max_in_flight: 2" in out and "+    max_in_flight: 4" in out
    assert cartridge.read_text(encoding="utf-8") == CARTRIDGE
    assert _git(cartridge.parent, "status", "--porcelain") == ""
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "1"


def test_set_on_a_tracked_file_commits_as_the_caller(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path, track=True)
    code = cox_settings.run_set(profile, "cartridge", "policy.dispatch.max_in_flight", "4", dry_run=False)
    assert code == 0
    assert "max_in_flight: 4" in cartridge.read_text(encoding="utf-8")
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "2"
    assert _git(cartridge.parent, "log", "-1", "--format=%an") == "Casey Caller"
    assert _git(cartridge.parent, "log", "-1", "--format=%s") == (
        "settings: set cartridge:policy.dispatch.max_in_flight = 4 (by Casey Caller)"
    )
    assert _git(cartridge.parent, "status", "--porcelain") == ""
    assert "committed it as Casey Caller" in capsys.readouterr().out


def test_set_on_a_machine_local_file_writes_without_a_commit(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path, track=False)
    code = cox_settings.run_set(profile, "cartridge", "policy.dispatch.max_in_flight", "4", dry_run=False)
    assert code == 0
    assert "max_in_flight: 4" in cartridge.read_text(encoding="utf-8")
    assert "no commit was made" in capsys.readouterr().out


def test_an_error_result_prints_and_writes_nothing(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path, track=True)
    code = cox_settings.run_set(profile, "cartridge", "policy.dispatch.max_in_flight", "many", dry_run=False)
    assert code == 1
    assert "expected int" in capsys.readouterr().out
    assert cartridge.read_text(encoding="utf-8") == CARTRIDGE
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "1"


def test_a_pat_only_setting_prints_the_notice_before_the_diff_and_proceeds(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path, track=True)
    code = cox_settings.run_set(profile, "cartridge", "policy.review_tier", "sonnet", dry_run=False)
    out = capsys.readouterr().out
    assert code == 0
    assert out.index("Pat-only") < out.index("--- a/cartridge.yaml")
    assert "review_tier: sonnet" in cartridge.read_text(encoding="utf-8")
    assert _git(cartridge.parent, "rev-list", "--count", "HEAD") == "2"
