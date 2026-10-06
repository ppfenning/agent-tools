from pathlib import Path

from agent_tools import cli, route


def test_the_chair_exports():
    assert route.should_export_board(True) is True


def test_a_lane_host_does_not_export():
    assert route.should_export_board(False) is False


def test_a_lease_held_elsewhere_names_another_machine_the_chair():
    assert route.is_chair_machine("laptop", "lane-1") is False


def test_a_lease_held_here_or_by_nobody_leaves_this_machine_the_chair():
    assert route.is_chair_machine("lane-1", "lane-1") is True
    assert route.is_chair_machine("", "lane-1") is True


def _rows(runs_dir, initiative_id, repo):
    return 0, [], {"id": initiative_id}


def test_a_lane_host_launch_reads_rows_and_makes_no_export_call():
    calls = []
    reads = []

    def read_rows(runs_dir, initiative_id, repo):
        reads.append(initiative_id)
        return _rows(runs_dir, initiative_id, repo)

    code, lines = cli._launch_rows_then_export(
        Path("/ws/runs"), "demo", None, False, read_rows=read_rows, export=lambda *args: calls.append(args) or [],
    )
    assert (code, lines, reads, calls) == (0, [], ["demo"], [])


def test_a_chair_launch_exports_the_whole_board_and_reports_skipped_rows():
    calls = []
    code, lines = cli._launch_rows_then_export(
        Path("/ws/runs"), "demo", None, True, read_rows=_rows, export=lambda *args: calls.append(args) or ["t1"],
    )
    assert (code, lines, calls) == (0, ["export: skipped 1 contentless row(s): t1"], [(Path("/ws/runs"), Path("/ws"))])


def test_a_rows_error_returns_before_any_export():
    calls = []
    code, lines = cli._launch_rows_then_export(
        Path("/ws/runs"), "demo", None, True,
        read_rows=lambda *args: (2, ["routing: no rows"], None), export=lambda *args: calls.append(args) or [],
    )
    assert (code, lines, calls) == (2, ["routing: no rows"], [])
