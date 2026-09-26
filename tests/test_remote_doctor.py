from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import auth_status_argv
from agent_tools.remote_doctor import auth_verdict, doctor_on_host

_NOT_LOGGED_IN = "claude auth: not logged in on the host (run claude auth login there)"


def _fake_run(auth_output, doctor_code=0):
    def run(argv):
        if argv[2] == "claude auth status":
            return 0, auth_output
        return doctor_code, "ok rows"

    return run


def test_auth_status_argv_is_the_claude_command():
    assert auth_status_argv() == ["claude", "auth", "status"]


def test_a_logged_in_status_has_no_verdict():
    assert auth_verdict('{"loggedIn": true}') is None


def test_a_logged_out_status_says_to_log_in_on_the_host():
    assert auth_verdict('{"loggedIn": false, "authMethod": "none"}') == _NOT_LOGGED_IN


def test_output_that_is_not_json_is_unreadable():
    assert auth_verdict("garbage") == "claude auth: status unreadable: garbage"


def test_doctor_on_host_fails_a_passing_doctor_when_claude_is_logged_out():
    host = LaneHost("box", "me@box", "/srv")
    code, rows = doctor_on_host(host, _fake_run('{"loggedIn": false}'))
    assert code == 1
    assert rows == ["ok rows", _NOT_LOGGED_IN]


def test_doctor_on_host_adds_an_ok_row_when_claude_is_logged_in():
    host = LaneHost("box", "me@box", "/srv")
    assert doctor_on_host(host, _fake_run('{"loggedIn": true}')) == (0, ["ok rows", "claude auth      ok"])
