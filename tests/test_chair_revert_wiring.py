"""The revert pieces wired into the chair's real deps: slug resolution, the CI reader, quarantine and resolve."""

from __future__ import annotations

import json
from pathlib import Path

from agent_tools import cli, commands  # noqa: F401  (cli imports commands first; keep the import order the suite uses)

GREEN = json.dumps([{"databaseId": 7, "status": "completed", "conclusion": "success"}])


def _checkout(tmp_path: Path, url: str = "git@github.com:o/r.git") -> str:
    (tmp_path / "co" / ".git").mkdir(parents=True)
    (tmp_path / "co" / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    return str(tmp_path / "co")


def test_gh_repo_resolves_a_checkout_and_passes_a_slug_through(tmp_path) -> None:
    assert cli._gh_repo(_checkout(tmp_path)) == "o/r"
    assert cli._gh_repo("o/r") == "o/r"


def test_main_ci_reader_reads_green() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, GREEN

    assert cli._main_ci_reader(runner)("o/r", "abc") == ("green", "")
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"


def test_main_ci_reader_turns_a_gh_failure_into_pending() -> None:
    state, output = cli._main_ci_reader(lambda argv: (1, "boom"))("o/r", "abc")
    assert state == "pending"
    assert output.startswith("RuntimeError: ")


def test_main_ci_reader_resolves_a_checkout_path(tmp_path) -> None:
    calls: list[list[str]] = []
    cli._main_ci_reader(lambda argv: (calls.append(argv), (0, GREEN))[1])(_checkout(tmp_path), "abc")
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"


def test_main_ci_reader_caches_per_repo_and_commit_until_cleared() -> None:
    calls: list[list[str]] = []
    read = cli._main_ci_reader(lambda argv: (calls.append(argv), (0, GREEN))[1])
    read("o/r", "abc")
    read("o/r", "abc")
    assert len(calls) == 1
    read("o/r", "def")
    assert len(calls) == 2
    read.cache.clear()
    read("o/r", "abc")
    assert len(calls) == 3


def test_slug_forge_open_pr_hands_gh_the_slug(tmp_path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "https://github.com/o/r/pull/9\n"

    forge = cli._SlugForge(cli.chair_revert_ports.GhForgePort(runner))
    assert forge.open_pr(_checkout(tmp_path), "revert-x", "t", "b") == "https://github.com/o/r/pull/9"
    assert calls[0][calls[0].index("--repo") + 1] == "o/r"


def test_slug_forge_delegates_wait_checks_and_merge_unchanged() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "ci\tpass\t1s\thttp://x\n"

    forge = cli._SlugForge(cli.chair_revert_ports.GhForgePort(runner))
    assert forge.wait_checks("http://pr") == []
    forge.merge("http://pr")
    assert calls == [["gh", "pr", "checks", "http://pr", "--watch"], ["gh", "pr", "merge", "http://pr", "--squash"]]


def test_revert_ports_gives_a_git_port_and_a_slug_forge_for_any_action() -> None:
    git, forge = cli._revert_ports(lambda argv: (0, ""))({})
    assert isinstance(git, cli.chair_revert_ports.GitRevertPort)
    assert isinstance(forge, cli._SlugForge)


def test_quarantine_phase_sets_state_once_per_matching_item_only() -> None:
    items = [
        {"id": "a", "initiative": "i1", "phase": "p1", "state": "ready"},
        {"id": "b", "initiative": "i1", "phase": "p1", "state": "landed"},
        {"id": "c", "initiative": "i1", "phase": "p2", "state": "ready"},
        {"id": "d", "initiative": "i2", "phase": "p1", "state": "ready"},
    ]
    seen: list[tuple[str, str, str, str]] = []
    quarantine = cli._quarantine_phase_with(lambda: items, lambda *args: seen.append(args), "holder")
    quarantine("i1", "p1", "main_red", "why")
    assert seen == [("i1", "a", "quarantined", "holder"), ("i1", "b", "quarantined", "holder")]


def _watch_row(initiative: str, phase: str, commit: str) -> dict:
    return {"kind": "revert_watch", "initiative": initiative, "phase": phase, "repo": "o/r", "pr": 1, "commit": commit}


def test_resolve_land_writes_one_row_per_pending_watch_of_that_phase() -> None:
    rows = [
        _watch_row("i1", "p1", "c1"),
        _watch_row("i1", "p1", "c2"),
        _watch_row("i1", "p1", "c3"),
        _watch_row("i1", "p2", "c4"),
        _watch_row("i2", "p1", "c5"),
        {"kind": "revert_watch_resolved", "commit": "c3", "outcome": "held"},
    ]
    written: list[dict] = []
    cli._resolve_land_with(lambda: rows, written.append)("i1", "p1", "reverted")
    assert [(w["kind"], w["commit"], w["outcome"]) for w in written] == [
        ("revert_watch_resolved", "c1", "reverted"),
        ("revert_watch_resolved", "c2", "reverted"),
    ]


def test_chair_run_deps_wires_the_revert_deps(tmp_path) -> None:
    deps = cli._chair_run_deps(tmp_path / "runs", {}, "chair", 1, "h", False, print, tmp_path / "profile.yaml", "files")
    assert deps.exec_deps.revert_ports is not None
    assert deps.exec_deps.quarantine_phase is not None
    assert deps.exec_deps.resolve_land is not None
    assert callable(deps.facts_deps.read_main_ci)
