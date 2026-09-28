from __future__ import annotations

from agent_tools import cli, console_screen


def test_console_once_parses_with_the_three_documented_defaults() -> None:
    a = cli.build_parser().parse_args(["console", "--once"])
    assert (a.once, a.runs_dir, a.work_dir, a.interval) == (True, "runs", ".", 5.0)


def test_console_once_prints_the_rendered_sections_and_returns_zero(monkeypatch, capsys) -> None:
    sections = {"drafts": [], "hosts": [], "lanes": [], "chair": [], "needs_chair": []}
    monkeypatch.setattr(console_screen, "gather", lambda runs_dir, work_dir, now: sections)
    rc = cli.main(["console", "--once"])
    assert rc == 0
    assert capsys.readouterr().out == "\n".join(console_screen.render(sections, -1, 120)) + "\n"
