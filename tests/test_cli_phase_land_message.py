import pytest

# The fixtures and the `_land` driver are the store-mode land harness; this file only asserts one output line.
from test_land_from_store import _land, _own_tempdir, repo, store  # noqa: F401

_REMOVED_LINE = "its items are already done, so there is no approved check"


@pytest.mark.parametrize("state", ["approved", "done"])
def test_a_store_mode_phase_land_never_claims_its_items_are_already_done(repo, tmp_path, monkeypatch, store, capsys, state):  # noqa: F811
    store.states = [state]
    rc, ran = _land(repo, tmp_path, monkeypatch, store, extra=("--phase", "seams"), phase=True)
    out = capsys.readouterr().out
    assert (rc, ran[-1]) == (0, "mark_done")
    assert "forge:" in out
    assert _REMOVED_LINE not in out
    assert "no approved check" not in out
