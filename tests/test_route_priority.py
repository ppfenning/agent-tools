"""`cox route priority`: the next value is pure, the write goes store first and file second."""

import pytest

from agent_tools import route, run_store
from agent_tools.cli import main


def _workspace(tmp_path, items: dict[str, str]):
    ws = tmp_path / "workspace"
    phase = ws / "work" / "demo" / "1-build"
    phase.mkdir(parents=True)
    (ws / "work" / "demo" / "initiative.md").write_text("---\ntitle: Demo\n---\nProse.\n")
    for name, text in items.items():
        (phase / f"{name}.md").write_text(text)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "team: acme\n"
        f"workspace_dir: {ws}\n"
        "harness_dir: /opt/coxswain-graphs\n"
        "cartridges_dir: /opt/cartridges\n"
        "provider_profile: /opt/providers/acme.yaml\n"
    )
    return profile, ws, phase


def _item(priority: int | None = None) -> str:
    key = "" if priority is None else f"priority: {priority}\n"
    return f"---\nstate: ready\ntitle: T\n{key}---\nBody.\n"


def _record_store(monkeypatch, path, events: list, result: bool = True) -> None:
    def upsert(runs_dir, row):
        events.append(("store", path.read_text(), row["extra"].get("priority")))
        return result

    monkeypatch.setattr(run_store, "upsert_row", upsert)


def _run(profile, *flags: str) -> int:
    return main(["route", "priority", "demo", *flags, "--profile", str(profile)])


# -- the pure core, literals only -----------------------------------------------------------


def test_next_priority_literals():
    assert route.next_priority(2, 5) == 5
    assert route.next_priority(2, "up") == 3
    assert route.next_priority(2, "down") == 1
    assert route.next_priority(0, "down") == -1
    assert route.next_priority(2, 0) == 0


def test_current_priority_is_the_highest_and_defaults_to_zero():
    assert route.current_priority([_item(2), _item(None), _item(-4)]) == 2
    assert route.current_priority([_item(-3), _item(-4)]) == -3
    assert route.current_priority([]) == 0


def test_priority_text_replaces_one_line_and_keeps_the_rest():
    attempts = "attempts:\n- run: r1\n  outcome: failed\n"
    original = f"---\nstate: ready\npriority: 2\n{attempts}title: T\n---\nBody.\n"
    assert route.priority_text(original, 3) == f"---\nstate: ready\npriority: 3\n{attempts}title: T\n---\nBody.\n"
    assert route.priority_text(_item(None), -1) == "---\nstate: ready\ntitle: T\npriority: -1\n---\nBody.\n"


# -- the verb, store and file ----------------------------------------------------------------


def test_set_writes_five_store_first(tmp_path, monkeypatch):
    profile, _ws, phase = _workspace(tmp_path, {"a": _item(None)})
    path, events = phase / "a.md", []
    _record_store(monkeypatch, path, events)
    assert _run(profile, "--set", "5") == 0
    assert events == [("store", _item(None), "5")]
    assert path.read_text() == "---\nstate: ready\ntitle: T\npriority: 5\n---\nBody.\n"


def test_up_from_two_writes_three_store_first(tmp_path, monkeypatch):
    profile, _ws, phase = _workspace(tmp_path, {"a": _item(2)})
    path, events = phase / "a.md", []
    _record_store(monkeypatch, path, events)
    assert _run(profile, "--up") == 0
    assert events == [("store", _item(2), "3")]
    assert path.read_text() == _item(3)


def test_down_from_two_writes_one_store_first(tmp_path, monkeypatch):
    profile, _ws, phase = _workspace(tmp_path, {"a": _item(2)})
    path, events = phase / "a.md", []
    _record_store(monkeypatch, path, events)
    assert _run(profile, "--down") == 0
    assert events == [("store", _item(2), "1")]
    assert path.read_text() == _item(1)


def test_one_call_leaves_every_item_equal_from_the_highest(tmp_path, monkeypatch):
    profile, _ws, phase = _workspace(tmp_path, {"a": _item(2), "b": _item(None)})
    _record_store(monkeypatch, phase / "a.md", [])
    assert _run(profile, "--up") == 0
    assert (phase / "a.md").read_text() == _item(3)
    assert (phase / "b.md").read_text() == "---\nstate: ready\ntitle: T\npriority: 3\n---\nBody.\n"


def test_a_failed_upsert_warns_and_still_writes_the_file(tmp_path, monkeypatch, capsys):
    profile, _ws, phase = _workspace(tmp_path, {"a": _item(2)})
    path, events = phase / "a.md", []
    _record_store(monkeypatch, path, events, result=False)
    assert _run(profile, "--down") == 0
    assert path.read_text() == _item(1)
    assert "store unavailable" in capsys.readouterr().out


def test_a_failed_file_write_after_a_good_upsert_exits_non_zero(tmp_path, monkeypatch, capsys):
    profile, _ws, phase = _workspace(tmp_path, {"a": _item(2)})
    path = phase / "a.md"
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: True)
    monkeypatch.setattr(type(path), "write_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert _run(profile, "--up") != 0
    assert "store recorded" in capsys.readouterr().out


def test_an_unknown_initiative_is_one_line_and_non_zero(tmp_path, monkeypatch, capsys):
    profile, _ws, _phase = _workspace(tmp_path, {"a": _item(2)})
    monkeypatch.setattr(run_store, "upsert_row", lambda runs_dir, row: pytest.fail("store must not be called"))
    assert main(["route", "priority", "nope", "--up", "--profile", str(profile)]) != 0
    assert capsys.readouterr().out.splitlines() == ["routing: unknown initiative nope"]


def test_exactly_one_flag_is_required(tmp_path, capsys):
    profile, _ws, _phase = _workspace(tmp_path, {"a": _item(2)})
    assert _run(profile) != 0
    with pytest.raises(SystemExit):
        _run(profile, "--up", "--down")
