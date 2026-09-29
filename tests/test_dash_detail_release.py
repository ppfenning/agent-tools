from agent_tools.dash_detail_release import release_facts

VERSION = "0.24.0"

MANIFEST = """
[coxswain]
version = "0.24.0"

[components]
tools = { repo = "ppfenning/coxswain-tools", tag = "v0.24.0" }
graphs = { repo = "ppfenning/coxswain-graphs", tag = "v0.24.0" }
cartridges = { repo = "ppfenning/coxswain-cartridges", tag = "v0.23.1" }
crew = { repo = "ppfenning/coxswain-crew", tag = "v0.12.0" }
"""

TAG_LINE = "v0.24.0 2026-09-28T20:53:38-04:00"

FORMULA = (
    'url "https://github.com/ppfenning/coxswain-tools/releases/download/'
    'v0.24.0/coxswain_tools-0.24.0.tar.gz"\n'
)

FORMULA_OLD = (
    'url "https://github.com/ppfenning/coxswain-tools/releases/download/'
    'v0.23.1/coxswain_tools-0.23.1.tar.gz"\n'
)


def test_release_facts_with_all_four_inputs_present():
    assert release_facts(VERSION, MANIFEST, TAG_LINE, FORMULA) == {
        "version": "0.24.0",
        "pinned": {
            "coxswain": "0.24.0",
            "tools": "v0.24.0",
            "graphs": "v0.24.0",
            "cartridges": "v0.23.1",
            "crew": "v0.12.0",
        },
        "last_cut": {"tag": "v0.24.0", "at": "2026-09-29T00:53:38+00:00"},
        "tap": {"version": "0.24.0", "matches_installed": True},
        "gate_progress": None,
    }


def test_release_facts_tap_mismatch_when_formula_lags_installed():
    facts = release_facts(VERSION, MANIFEST, TAG_LINE, FORMULA_OLD)
    assert facts["tap"] == {"version": "0.23.1", "matches_installed": False}


def test_release_facts_tap_is_none_without_a_formula():
    facts = release_facts(VERSION, MANIFEST, TAG_LINE, None)
    assert facts["tap"] is None
