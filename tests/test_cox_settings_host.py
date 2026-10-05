"""`cox settings set host <host>.capacity N` writes through `cli._host_capacity` and nothing else."""

from __future__ import annotations

import argparse

import pytest

from agent_tools import cli, cox_settings


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    seen: list[argparse.Namespace] = []

    def fake(a: argparse.Namespace) -> int:
        seen.append(a)
        return 0

    monkeypatch.setattr(cli, "_host_capacity", fake)
    return seen


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("workspace_dir: /nowhere\n", encoding="utf-8")
    return path


def test_write_reaches_host_capacity_with_parsed_host_and_n(calls, profile):
    code = cox_settings.run_set(profile, "host", "web.1.capacity", "4", dry_run=False)
    assert code == 0
    assert [(a.name, a.n, a.profile, a.json) for a in calls] == [("web.1", 4, str(profile), False)]


def test_dry_run_prints_the_command_and_writes_nothing(calls, profile, capsys, tmp_path):
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    code = cox_settings.run_set(profile, "host", "web.1.capacity", "4", dry_run=True)
    assert code == 0
    assert capsys.readouterr().out.strip() == "cox host capacity web.1 4"
    assert calls == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("key", ["web.size", "capacity", ".capacity", "web.capacity.x"])
def test_malformed_key_is_rejected(calls, profile, capsys, key):
    assert cox_settings.run_set(profile, "host", key, "4", dry_run=False) == 1
    assert capsys.readouterr().out.startswith("error:")
    assert calls == []


@pytest.mark.parametrize("value", ["four", "4.5", ""])
def test_non_integer_n_is_rejected(calls, profile, capsys, value):
    assert cox_settings.run_set(profile, "host", "web.capacity", value, dry_run=False) == 1
    assert capsys.readouterr().out.startswith("error:")
    assert calls == []


def test_parse_host_capacity_splits_on_the_last_dot():
    assert cox_settings.parse_host_capacity("web.1.capacity", "4") == ("web.1", 4)
    assert cox_settings.parse_host_capacity("box.capacity", "0") == ("box", 0)
