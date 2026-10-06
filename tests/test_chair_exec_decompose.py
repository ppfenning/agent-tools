import pytest

from agent_tools.chair_exec import argv_for
from agent_tools.chair_plan_fill import plan_fill
from agent_tools.cli import build_parser


def test_a_decompose_action_with_a_host_passes_on_and_the_host_name():
    action = {"kind": "launch_decompose", "intake_ids": ["i1"], "host": "jarvis"}
    argv = argv_for(action, "init-1")
    assert argv is not None
    assert argv[argv.index("--on") + 1] == "jarvis"


def test_a_decompose_action_without_a_host_passes_no_on():
    argv = argv_for({"kind": "launch_decompose", "intake_ids": ["i1"]}, "init-1")
    assert argv is not None
    assert "--on" not in argv


def test_the_cli_parses_the_argv_for_a_local_decompose():
    argv = argv_for({"kind": "launch_decompose", "intake_ids": ["work/intake/i1.md"]}, "init-1")
    assert argv is not None
    assert build_parser().parse_args(argv[1:]).initiative_id == "init-1"


def test_the_cli_refuses_a_hosted_decompose_so_the_planner_keeps_hosted_decompose_off():
    # When `cox route launch decompose` gains --on, this parse succeeds: wire the host path, then default it on.
    argv = argv_for({"kind": "launch_decompose", "intake_ids": ["work/intake/i1.md"], "host": "jarvis"}, "init-1")
    assert argv is not None
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv[1:])
    dispatch = {"max_in_flight": 4, "live_runs": 0, "hosts": [{"name": "jarvis", "live_runs": 0}]}
    facts = {
        "dispatch": dispatch, "initiatives": [], "intake": ["i1", "i2"],
        "work_store_ready": False, "sources_configured": False,
    }
    assert all("host" not in a for a in plan_fill(facts, 0))
