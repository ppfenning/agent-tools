from __future__ import annotations

from agent_tools import chair_cap, chair_capacity, cli


def _cartridge(root, name: str, text: str) -> None:
    (root / name).mkdir()
    (root / name / "cartridge.yaml").write_text(text, encoding="utf-8")


def test_cap_is_the_positive_int_at_policy_dispatch_max_in_flight() -> None:
    assert chair_cap.cap_from_cartridge("policy:\n  dispatch:\n    max_in_flight: 8\n") == 8


def test_no_policy_gives_none() -> None:
    assert chair_cap.cap_from_cartridge("team: pat\n") is None


def test_a_bool_a_zero_and_a_missing_text_give_none() -> None:
    texts = ["policy:\n  dispatch:\n    max_in_flight: true\n", "policy:\n  dispatch:\n    max_in_flight: 0\n", None]
    assert [chair_cap.cap_from_cartridge(t) for t in texts] == [None, None, None]


def test_extends_is_a_name_or_a_list_of_names() -> None:
    assert (chair_cap.extends_of("extends: local\n"), chair_cap.extends_of("extends: [a, b]\n"), chair_cap.extends_of("team: x\n")) == (
        ["local"],
        ["a", "b"],
        [],
    )


def test_a_team_without_a_cap_takes_its_extends_parent_cap(tmp_path) -> None:
    _cartridge(tmp_path, "pat", "team: pat\nextends: local\n")
    _cartridge(tmp_path, "local", "policy:\n  dispatch:\n    max_in_flight: 8\n")
    profile = {"cartridges_dir": str(tmp_path), "team": "pat"}
    assert cli._chair_max_in_flight(tmp_path / "runs", profile) == 8


def test_local_lanes_is_any_or_decompose_and_nothing_else() -> None:
    texts = [f"policy:\n  dispatch:\n    local_lanes: {v}\n" for v in ("decompose", "any", "epics", "1")] + ["team: x\n", None]
    assert [chair_cap.local_lanes_from_cartridge(t) for t in texts] == ["decompose", "any", None, None, None, None]


def test_local_lanes_follows_the_extends_chain_and_defaults_to_any(tmp_path) -> None:
    _cartridge(tmp_path, "pat", "team: pat\nextends: local\n")
    _cartridge(tmp_path, "local", "policy:\n  dispatch:\n    local_lanes: decompose\n")
    _cartridge(tmp_path, "bare", "team: bare\n")
    reads = [chair_capacity.chair_local_lanes({"cartridges_dir": str(tmp_path), "team": t}) for t in ("pat", "bare")]
    assert [*reads, chair_capacity.chair_local_lanes({})] == ["decompose", "any", "any"]


def test_with_nothing_anywhere_the_cap_is_three(tmp_path) -> None:
    _cartridge(tmp_path, "pat", "team: pat\nextends: pat\n")
    profile = {"cartridges_dir": str(tmp_path), "team": "pat"}
    assert (cli._chair_max_in_flight(tmp_path / "runs", profile), cli._chair_max_in_flight(tmp_path / "runs", {})) == (3, 3)
