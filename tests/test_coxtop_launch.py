from agent_tools.coxtop_launch import Exec, Fallback, decide, launch


class _Stream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_installed_and_tty_execs():
    assert decide("/usr/bin/coxtop", True, True) == Exec("/usr/bin/coxtop")


def test_installed_and_no_tty_falls_back():
    assert decide("/usr/bin/coxtop", False, True) == Fallback()


def test_missing_and_tty_falls_back():
    assert decide(None, True, True) == Fallback()


def test_missing_and_no_tty_falls_back():
    assert decide(None, False, False) == Fallback()


def test_launch_execs_coxtop_on_a_terminal_and_falls_back_without_one():
    calls = []
    which = {"coxtop": "/usr/bin/coxtop"}.get
    tty = _Stream(True)

    assert launch(which, lambda path, argv: calls.append((path, argv)), tty, tty) is True
    assert calls == [("/usr/bin/coxtop", ["/usr/bin/coxtop"])]

    calls.clear()
    assert launch(which, lambda path, argv: calls.append((path, argv)), _Stream(False), tty) is False
    assert calls == []
