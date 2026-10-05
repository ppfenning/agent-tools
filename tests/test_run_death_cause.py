from agent_tools.run_death_cause import death_cause, schema_versions

REFUSAL = "database is at schema version 12, newest known migration is 11"


def test_refusal_line_is_a_schema_version_death():
    assert death_cause(REFUSAL) == "schema_version"


def test_refusal_inside_a_traceback_is_still_a_schema_version_death():
    tail = (
        "Traceback (most recent call last):\n"
        '  File "harness/store.py", line 40, in open_store\n'
        f"RuntimeError: {REFUSAL}\n"
    )
    assert death_cause(tail) == "schema_version"


def test_unrelated_traceback_has_no_cause():
    tail = "Traceback (most recent call last):\n  File \"x.py\", line 1\nKeyError: 'id'\n"
    assert death_cause(tail) is None


def test_empty_text_has_no_cause():
    assert death_cause("") is None


def test_schema_versions_names_both_numbers():
    assert schema_versions(REFUSAL) == (12, 11)
