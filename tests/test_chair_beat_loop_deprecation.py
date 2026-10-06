import subprocess
import sys

from agent_tools import chair


def _gone(pid, sig):
    raise ProcessLookupError(pid)


class _Clock:
    def sleep(self, seconds):
        raise AssertionError("must not sleep when the pid is already gone")


def test_notice_on_stderr_and_exit_unchanged(tmp_path, capsys):
    code = chair.beat_loop("s", 4242, runs_dir=tmp_path, clock=_Clock(), alive=_gone, environ={})
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err.strip() == chair.BEAT_LOOP_DEPRECATION
    assert captured.out == ""


def test_exited_pid_never_beats(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(chair, "beat", lambda *a, **k: calls.append(a) or (None, "x"))
    code = chair.beat_loop("s", 4242, runs_dir=tmp_path, clock=_Clock(), alive=_gone, environ={})
    assert code == 0
    assert calls == []


def test_notice_names_the_change_and_the_removal():
    assert "\n" not in chair.BEAT_LOOP_DEPRECATION
    assert "beats its own lease" in chair.BEAT_LOOP_DEPRECATION
    assert "removed in the next release" in chair.BEAT_LOOP_DEPRECATION


def test_help_says_deprecated():
    result = subprocess.run([sys.executable, "-m", "agent_tools.chair", "--help"], capture_output=True, text=True, check=True)
    assert "DEPRECATED" in result.stdout
