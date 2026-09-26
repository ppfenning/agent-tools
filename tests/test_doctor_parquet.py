from agent_tools import cli, doctor, run_store


def test_the_traces_line_names_the_endpoint_when_the_profile_has_a_block(tmp_path, monkeypatch):
    provider = tmp_path / "provider.yaml"
    provider.write_text(
        "traces_url: s3://coxswain/traces\n"
        "object_store:\n  endpoint: http://100.71.8.33:3900\n  access_key_env: A\n  secret_key_env: S\n"
    )
    seen = []
    monkeypatch.setattr(run_store, "parquet_readable",
                        lambda root, harness: seen.append(root) or run_store.ParquetCheck(True, "ok"))
    profile = {"workspace_dir": str(tmp_path / "ws"), "provider_profile": str(provider)}
    assert cli._parquet_traces_line(profile) == "parquet traces: readable via http://100.71.8.33:3900"
    assert seen[0].url == "s3://coxswain/traces"
    assert seen[0].object_store["endpoint"] == "http://100.71.8.33:3900"


def test_the_traces_line_is_unchanged_without_a_block(tmp_path, monkeypatch):
    provider = tmp_path / "provider.yaml"
    provider.write_text("traces_url: s3://coxswain/traces\n")
    monkeypatch.setattr(run_store, "parquet_readable", lambda root, harness: run_store.ParquetCheck(True, "ok"))
    profile = {"workspace_dir": str(tmp_path / "ws"), "provider_profile": str(provider)}
    assert cli._parquet_traces_line(profile) == "parquet traces: readable"


def test_the_traces_line_names_an_unset_env_var_rather_than_an_unreachable_root(tmp_path, monkeypatch):
    provider = tmp_path / "provider.yaml"
    provider.write_text(
        "traces_url: s3://coxswain/traces\n"
        "object_store:\n  endpoint: http://100.71.8.33:3900\n  access_key_env: GARAGE_ACCESS_KEY_ID\n"
    )
    monkeypatch.delenv("GARAGE_ACCESS_KEY_ID", raising=False)
    profile = {"workspace_dir": str(tmp_path / "ws"), "provider_profile": str(provider)}
    assert cli._parquet_traces_line(profile) == (
        "parquet traces: not readable (object_store names env var GARAGE_ACCESS_KEY_ID, which is not set)"
        " via http://100.71.8.33:3900"
    )


def test_parquet_line_names_the_endpoint_after_a_failure_too():
    assert doctor.parquet_line(False, "something new", "http://h:1") == "parquet traces: not readable (something new) via http://h:1"
