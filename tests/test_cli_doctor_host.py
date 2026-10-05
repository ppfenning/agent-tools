import pytest

from agent_tools import cli
from agent_tools.cli import main

SSH_BOUND = ["ssh", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
_HOSTS = """team: t
lane_hosts:
  - name: box
    ssh: me@box.example
    workspace_dir: /srv/ws
  - name: gpu
    ssh: me@gpu.example
    workspace_dir: /srv/gpu
"""


def _profile(tmp_path, text=_HOSTS):
    p = tmp_path / "profile.yaml"
    p.write_text(text)
    return str(p)


def _fake_run(monkeypatch, code=0, output="ok  profile\nok  git\n"):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[-1] == "claude auth status":
            return 0, '{"loggedIn": true}'
        return code, output

    monkeypatch.setattr(cli, "_run_ssh", run)
    return calls


def test_a_known_host_prints_its_rows_under_a_header_and_exits_zero(tmp_path, monkeypatch, capsys):
    calls = _fake_run(monkeypatch)
    rc = main(["setup", "doctor", "--profile", _profile(tmp_path), "--host", "box"])
    assert rc == 0
    assert capsys.readouterr().out == "doctor on box (me@box.example)\nok  profile\nok  git\nclaude auth      ok\n"
    assert calls == [
        [*SSH_BOUND, "me@box.example", "cox setup doctor"],
        [*SSH_BOUND, "me@box.example", "claude auth status"],
    ]


def test_a_failing_remote_doctor_prints_its_rows_and_exits_non_zero(tmp_path, monkeypatch, capsys):
    _fake_run(monkeypatch, code=1, output="FAIL  git\n")
    rc = main(["setup", "doctor", "--profile", _profile(tmp_path), "--host", "gpu"])
    assert rc == 1
    assert capsys.readouterr().out == "doctor on gpu (me@gpu.example)\nFAIL  git\nclaude auth      ok\n"


def test_an_unknown_host_names_the_configured_ones_and_runs_nothing(tmp_path, monkeypatch, capsys):
    calls = _fake_run(monkeypatch)
    rc = main(["setup", "doctor", "--profile", _profile(tmp_path), "--host", "nope"])
    assert rc == 1
    assert capsys.readouterr().out == "unknown lane host: nope\nconfigured: box, gpu\n"
    assert calls == []


def test_a_profile_with_no_lane_hosts_says_none_are_configured(tmp_path, monkeypatch, capsys):
    _fake_run(monkeypatch)
    rc = main(["setup", "doctor", "--profile", _profile(tmp_path, "team: t\n"), "--host", "box"])
    assert rc == 1
    assert capsys.readouterr().out == "unknown lane host: box\nconfigured: none\n"


def test_a_malformed_lane_hosts_entry_exits_non_zero_with_its_message(tmp_path, monkeypatch, capsys):
    calls = _fake_run(monkeypatch)
    bad = "team: t\nlane_hosts:\n  - name: box\n"
    rc = main(["setup", "doctor", "--profile", _profile(tmp_path, bad), "--host", "box"])
    assert rc == 1
    assert "lane_hosts[0]" in capsys.readouterr().out
    assert calls == []


def test_a_missing_profile_exits_non_zero(tmp_path, monkeypatch, capsys):
    _fake_run(monkeypatch)
    rc = main(["setup", "doctor", "--profile", str(tmp_path / "absent.yaml"), "--host", "box"])
    assert rc == 1
    assert "cannot read profile" in capsys.readouterr().out


def test_without_host_the_local_doctor_runs_and_ssh_is_never_called(tmp_path, monkeypatch, capsys):
    def boom(argv):
        raise AssertionError("ssh must not run without --host")

    monkeypatch.setattr(cli, "_run_ssh", boom)
    main(["setup", "doctor", "--profile", str(tmp_path / "absent.yaml"), "--repo", str(tmp_path)])
    assert "doctor on" not in capsys.readouterr().out


@pytest.mark.parametrize("output,rows", [("", []), ("a\nb\n", ["a", "b"])])
def test_doctor_on_host_returns_the_code_and_the_split_rows(output, rows):
    from agent_tools.lane_hosts import LaneHost
    from agent_tools.remote_doctor import doctor_on_host

    host = LaneHost("box", "me@box", "/srv")
    auth = '{"loggedIn": true}'
    run = lambda argv: (0, auth) if argv[-1] == "claude auth status" else (3, output)  # noqa: E731
    assert doctor_on_host(host, run) == (3, [*rows, "claude auth      ok"])
