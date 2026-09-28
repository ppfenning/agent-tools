from __future__ import annotations

from agent_tools import chair_service_host


def test_install_steps_gives_the_exact_argv_and_run_flags_in_order() -> None:
    repos = {"coxswain": "git@example.com:coxswain.git", "coxswain-tools": "git@example.com:coxswain-tools.git"}
    assert chair_service_host.install_steps("jarvis", "/r", repos, "git@example.com:workspace.git", "chair") == [
        (["ssh", "jarvis", "test -d /r/coxswain && git -C /r/coxswain pull --ff-only || "
                            "git clone git@example.com:coxswain.git /r/coxswain"], True),
        (["ssh", "jarvis", "test -d /r/coxswain-tools && git -C /r/coxswain-tools pull --ff-only || "
                            "git clone git@example.com:coxswain-tools.git /r/coxswain-tools"], True),
        (["ssh", "jarvis", "test -d /r/workspace && git -C /r/workspace pull --ff-only || "
                            "git clone git@example.com:workspace.git /r/workspace"], True),
        (["ssh", "jarvis", "uv tool install --force --editable '/r/coxswain-tools[traces,parquet,lake,postgres]'"], True),
        (["ssh", "jarvis", "cox chair service --install --label chair"], True),
        (["sudo", "loginctl", "enable-linger", "<user>"], False),
        (["ssh", "jarvis", "systemctl --user enable --now coxswain-chair.service"], False),
    ]


def test_the_linger_and_enable_steps_are_never_marked_to_run() -> None:
    steps = chair_service_host.install_steps("jarvis", "/r", {"coxswain": "u"}, "wu", "chair")
    assert [run for _argv, run in steps[-2:]] == [False, False]


def test_status_line_with_every_value_present() -> None:
    assert chair_service_host.status_line("jarvis", "active", "tick 12 ok", "2026-09-28T00:00Z") == (
        "jarvis: active; tick 12 ok; 2026-09-28T00:00Z"
    )


def test_status_line_with_the_unit_missing() -> None:
    assert chair_service_host.status_line("jarvis", None, "no chair line", None) == (
        "jarvis: no unit; no chair line; no tick"
    )
