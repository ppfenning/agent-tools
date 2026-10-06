from agent_tools.towpath_launch import Exec, Fallback, choose_binary, decide, launch


class _Stream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_installed_and_tty_execs():
    assert decide("/usr/bin/towpath", True, True) == Exec("/usr/bin/towpath")


def test_installed_and_no_tty_falls_back():
    assert decide("/usr/bin/towpath", False, True) == Fallback()


def test_missing_and_tty_falls_back():
    assert decide(None, True, True) == Fallback()


def test_missing_and_no_tty_falls_back():
    assert decide(None, False, False) == Fallback()


def test_towpath_on_path_is_chosen_over_coxtop():
    found = {"towpath": "/usr/bin/towpath", "coxtop": "/usr/bin/coxtop"}
    assert choose_binary(found) == "/usr/bin/towpath"


def test_only_coxtop_on_path_is_chosen():
    assert choose_binary({"towpath": None, "coxtop": "/usr/bin/coxtop"}) == "/usr/bin/coxtop"


def test_neither_on_path_chooses_nothing_and_launch_falls_back():
    calls = []
    tty = _Stream(True)

    assert choose_binary({"towpath": None, "coxtop": None}) is None
    assert launch({}.get, lambda path, argv: calls.append((path, argv)), tty, tty) is False
    assert calls == []


def test_non_terminal_falls_back_even_with_both_on_path():
    calls = []
    which = {"towpath": "/usr/bin/towpath", "coxtop": "/usr/bin/coxtop"}.get

    assert launch(which, lambda path, argv: calls.append((path, argv)), _Stream(False), _Stream(False)) is False
    assert calls == []


def test_launch_execs_towpath_on_a_terminal_and_falls_back_without_one():
    calls = []
    which = {"towpath": "/usr/bin/towpath"}.get
    tty = _Stream(True)

    assert launch(which, lambda path, argv: calls.append((path, argv)), tty, tty) is True
    assert calls == [("/usr/bin/towpath", ["/usr/bin/towpath"])]

    calls.clear()
    assert launch(which, lambda path, argv: calls.append((path, argv)), _Stream(False), tty) is False
    assert calls == []
