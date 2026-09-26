from __future__ import annotations

from pathlib import Path

import pytest

from agent_tools import chair_service, cli


def test_unit_text_with_an_environment_file_loads_it_and_runs_the_labelled_chair() -> None:
    text = chair_service.unit_text("/usr/local/bin/cox", "/ws", "chair", 60, "/home/x/.config/agent-tools/garage.env")
    assert "EnvironmentFile=-/home/x/.config/agent-tools/garage.env" in text
    assert "ExecStart=/usr/local/bin/cox chair run --label chair --interval 60" in text


def test_unit_text_without_an_environment_file_has_no_such_line() -> None:
    assert "EnvironmentFile" not in chair_service.unit_text("/usr/local/bin/cox", "/ws", "chair", 60, None)


def test_unsafe_values_names_what_systemd_would_split_or_expand() -> None:
    assert chair_service.unsafe_values({"--label": "a b", "cox": "/bin/cox", "x": "50%", "y": "$HOME", "z": ""}) == [
        "--label", "x", "y", "z",
    ]


def test_export_lines_numbers_the_shell_style_assignments() -> None:
    assert chair_service.export_lines("A=1\nexport B=2\n# c\n  export C=3\n") == [2, 4]


def test_chair_run_accepts_the_flags_the_unit_passes_it() -> None:
    a = cli.build_parser().parse_args(["chair", "run", "--label", "chair", "--interval", "60", "--profile", "/p.yaml"])
    assert (a.label, a.interval, a.profile) == ("chair", 60.0, "/p.yaml")


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/local/bin/cox")
    (tmp_path / "profile.yaml").write_text("workspace_dir: /ws\n")
    return tmp_path


def _service(*flags: str) -> int:
    return cli.main(["chair", "service", *flags])


def test_install_writes_the_unit_and_prints_the_three_systemctl_lines(home: Path, capsys) -> None:
    assert _service("--install", "--label", "chair", "--profile", str(home / "profile.yaml")) == 0
    out = capsys.readouterr().out
    path = chair_service.unit_path(home)
    assert path.read_text().splitlines()[4:6] == [
        f"ExecStart=/usr/local/bin/cox chair run --label chair --interval 60 --profile {home}/profile.yaml",
        "WorkingDirectory=/ws",
    ]
    assert out == f"{path}\n" + "\n".join(chair_service.systemctl_lines()) + "\n"


def test_install_expands_a_tilde_workspace_to_an_absolute_working_directory(home: Path) -> None:
    (home / "profile.yaml").write_text("workspace_dir: ~/ws\n")
    assert _service("--install", "--profile", str(home / "profile.yaml")) == 0
    assert f"WorkingDirectory={home}/ws\n" in chair_service.unit_path(home).read_text()


def test_status_prints_what_install_wrote(home: Path, capsys) -> None:
    _service("--install", "--profile", str(home / "profile.yaml"))
    capsys.readouterr()
    assert _service("--status") == 0
    assert capsys.readouterr().out == chair_service.unit_path(home).read_text()


def test_status_before_any_install_names_the_missing_path(home: Path, capsys) -> None:
    assert _service("--status") == 0
    assert capsys.readouterr().out == f"no unit at {chair_service.unit_path(home)}\n"


def test_install_refuses_when_cox_is_not_on_path(home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert _service("--install", "--profile", str(home / "profile.yaml")) == 2
    assert capsys.readouterr().out == "chair service: cox is not on PATH\n"
    assert not chair_service.unit_path(home).exists()


def test_install_refuses_a_label_systemd_would_split(home: Path, capsys) -> None:
    assert _service("--install", "--label", "my chair", "--profile", str(home / "profile.yaml")) == 2
    assert capsys.readouterr().out.startswith("chair service: systemd would split or expand --label;")
    assert not chair_service.unit_path(home).exists()


def test_install_loads_garage_env_only_when_that_file_exists(home: Path) -> None:
    garage = home / ".config/agent-tools/garage.env"
    garage.parent.mkdir(parents=True)
    garage.write_text("A=b\n")
    _service("--install", "--profile", str(home / "profile.yaml"))
    assert f"EnvironmentFile=-{garage}" in chair_service.unit_path(home).read_text()


def test_install_refuses_an_environment_file_with_export_lines(home: Path, capsys) -> None:
    garage = home / ".config/agent-tools/garage.env"
    garage.parent.mkdir(parents=True)
    garage.write_text("export A=b\n")
    assert _service("--install", "--profile", str(home / "profile.yaml")) == 2
    assert capsys.readouterr().out.startswith(f"chair service: {garage} has export lines (1)")
    assert not chair_service.unit_path(home).exists()


def test_service_with_neither_flag_prints_its_help(home: Path, capsys) -> None:
    assert _service() == 0
    assert "usage: cox chair service" in capsys.readouterr().out
    assert not chair_service.unit_path(home).exists()
