"""Commands that take an initiative accept its short id: the edge resolves it first, with run_store faked."""

from pathlib import Path

import pytest

from agent_tools import cli, draft_apply, run_store

SLUGS = {"I412": "demo-slug"}


@pytest.fixture
def calls(monkeypatch, tmp_path: Path) -> list[tuple]:
    seen: list[tuple] = []
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: SLUGS.get(token, token))
    monkeypatch.setattr(draft_apply, "approve", lambda work, slug, task, by, store, **kw: seen.append(("approve", slug)) or 0)
    monkeypatch.setattr(draft_apply, "decline", lambda work, slug, reason, by, store, **kw: seen.append(("decline", slug)) or 0)
    monkeypatch.setattr(cli, "_lake_provider", lambda a: ({},))
    monkeypatch.setattr(cli, "_runs_dir_for_land", lambda a: (tmp_path / "runs", None))
    return seen


def test_approve_short_id_acts_on_the_mapped_slug(calls):
    assert cli.main(["route", "approve", "I412"]) == 0
    assert calls == [("approve", "demo-slug")]


def test_approve_slug_acts_on_the_slug(calls):
    assert cli.main(["route", "approve", "some-slug"]) == 0
    assert calls == [("approve", "some-slug")]


def test_decline_short_id_acts_on_the_mapped_slug(calls):
    assert cli.main(["route", "decline", "I412", "--reason", "no"]) == 0
    assert calls == [("decline", "demo-slug")]


def test_launch_short_id_names_the_work_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(run_store, "resolve_initiative", lambda runs_dir, token: SLUGS.get(token, token))
    runs = tmp_path / "runs"
    assert cli._initiative_path(runs, "I412") == tmp_path / "work" / "demo-slug"
    assert cli._initiative_path(runs, "work/some-slug") == Path("work/some-slug")
    assert cli._initiative_path(runs, "some-slug") == Path("some-slug")
