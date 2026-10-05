from agent_tools.chair_read_schema_deaths import schema_deaths

LINE = "error: database is at schema version 7, newest known migration is 9"


def test_two_newest_runs_both_ending_in_the_refusal_give_the_initiative_with_both_run_ids():
    runs = {"x": [("x-2", True, LINE), ("x-1", True, LINE)]}
    assert schema_deaths(runs) == {"x": ["x-2", "x-1"]}


def test_only_the_newest_dying_on_the_refusal_gives_absent():
    runs = {"x": [("x-2", True, LINE), ("x-1", True, "boom")]}
    assert schema_deaths(runs) == {}


def test_a_newer_third_run_that_did_not_die_on_it_gives_absent():
    runs = {"x": [("x-3", True, "boom"), ("x-2", True, LINE), ("x-1", True, LINE)]}
    assert schema_deaths(runs) == {}


def test_a_still_running_newest_run_gives_absent():
    runs = {"x": [("x-2", False, LINE), ("x-1", True, LINE)]}
    assert schema_deaths(runs) == {}


def test_a_single_run_gives_absent():
    assert schema_deaths({"x": [("x-1", True, LINE)]}) == {}
