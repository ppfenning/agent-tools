import sys
import types

from agent_tools import schema


def test_cell_returns_the_version_when_present():
    assert schema.cell("1.0") == "1.0"


def test_cell_returns_a_question_mark_for_none():
    assert schema.cell(None) == "?"


def test_status_is_ok_when_every_known_major_agrees():
    state, detail = schema.status({"cartridges": "1.0", "graphs": "1.0", "tools": "1.0"})
    assert (state, detail) == ("ok", "cartridges 1.0, graphs 1.0, tools 1.0")


def test_status_warns_naming_only_the_disagreeing_component():
    state, detail = schema.status({"cartridges": "1.0", "graphs": "2.0", "tools": "1.0"})
    assert (state, detail) == ("WARN", "graphs 2.0")


def test_a_none_entry_never_triggers_a_warn_and_never_appears_in_either_message():
    state, detail = schema.status({"cartridges": "1.0", "graphs": None, "tools": "1.0"})
    assert state == "ok"
    assert "graphs" not in detail
    assert detail == "cartridges 1.0, tools 1.0"


def test_cartridges_schema_resolves_from_the_nearest_ancestor_of_provider_profile(tmp_path):
    core_dir = tmp_path / "checkout" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "__init__.py").write_text('SCHEMA_VERSION = "1.2"\n')
    provider_profile = tmp_path / "checkout" / "providers" / "x.yaml"
    provider_profile.parent.mkdir(parents=True)
    provider_profile.write_text("command: fakeprovider\n")
    (tmp_path / "workspace" / "cartridges").mkdir(parents=True)
    assert schema.cartridges_schema(str(provider_profile)) == "1.2"


def test_cartridges_schema_resolves_from_a_skills_root_when_provider_profile_misses(tmp_path):
    core_dir = tmp_path / "checkout" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "__init__.py").write_text('SCHEMA_VERSION = "1.2"\n')
    skills_root = tmp_path / "checkout" / "skills-plugins" / "team"
    skills_root.mkdir(parents=True)
    assert schema.cartridges_schema(tmp_path / "provider.yaml", [skills_root]) == "1.2"


def test_cartridges_schema_returns_none_with_no_core_init_under_any_ancestor(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "_fallback_import", lambda module, name: None)
    provider_profile = tmp_path / "checkout" / "providers" / "x.yaml"
    provider_profile.parent.mkdir(parents=True)
    skills_root = tmp_path / "checkout" / "skills-plugins" / "team"
    skills_root.mkdir(parents=True)
    assert schema.cartridges_schema(str(provider_profile), [skills_root]) is None
    assert schema.cell(schema.cartridges_schema(str(provider_profile), [skills_root])) == "?"


def test_graphs_schema_reads_the_constant_from_the_harness_checkout(tmp_path):
    harness_dir = tmp_path / "harness"
    (harness_dir / "harness").mkdir(parents=True)
    (harness_dir / "harness" / "__init__.py").write_text('CORE_SCHEMA = "1.0"\n')
    assert schema.graphs_schema(harness_dir) == "1.0"


def test_graphs_schema_returns_none_when_the_checkout_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "_fallback_import", lambda module, name: None)
    assert schema.graphs_schema(tmp_path / "harness") is None


def test_the_fallback_import_reads_the_constant_from_an_importable_module(monkeypatch):
    module = types.ModuleType("fake_schema_module")
    module.SCHEMA_VERSION = "9.9"
    monkeypatch.setitem(sys.modules, "fake_schema_module", module)
    assert schema._fallback_import("fake_schema_module", "SCHEMA_VERSION") == "9.9"
    assert schema._fallback_import("no_such_module_for_schema_test", "SCHEMA_VERSION") is None
