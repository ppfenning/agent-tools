from agent_tools.cli import _initiative_of_phase


def _ticket(root, initiative, phase):
    d = root / initiative / phase
    d.mkdir(parents=True)
    (d / "t.md").write_text("---\nid: t\n---\n")


def test_a_shared_phase_name_resolves_to_the_runs_own_initiative(tmp_path):
    _ticket(tmp_path, "alpha", "fix")
    _ticket(tmp_path, "beta", "fix")
    assert _initiative_of_phase(tmp_path, "fix", "beta-4") == "beta"


def test_a_shared_phase_name_without_a_run_names_no_initiative(tmp_path):
    _ticket(tmp_path, "alpha", "fix")
    _ticket(tmp_path, "beta", "fix")
    assert _initiative_of_phase(tmp_path, "fix") is None


def test_a_run_whose_initiative_lacks_the_phase_falls_back_to_the_one_that_has_it(tmp_path):
    _ticket(tmp_path, "alpha", "fix")
    assert _initiative_of_phase(tmp_path, "fix", "gamma-2") == "alpha"
