import re

import pytest

from agent_tools import lake_config, run_store
from agent_tools.store_url import TracesRoot

BLOCK = {
    "endpoint": "http://garage.lan:3900",
    "region": "garage",
    "access_key_env": "GARAGE_ACCESS_KEY_ID",
    "secret_key_env": "GARAGE_SECRET_ACCESS_KEY",
    "path_style": True,
}
ENV = {"GARAGE_ACCESS_KEY_ID": "AK", "GARAGE_SECRET_ACCESS_KEY": "SK"}


def test_s3_options_from_the_block_and_the_environment():
    assert run_store.s3_options(BLOCK, ENV) == {
        "scheme": "http",
        "endpoint_override": "garage.lan:3900",
        "region": "garage",
        "access_key": "AK",
        "secret_key": "SK",
        "force_virtual_addressing": False,
    }


def test_an_endpoint_without_a_scheme_is_https():
    assert run_store.s3_options({"endpoint": "s3.example.com"}, {})["scheme"] == "https"


def test_a_literal_secret_key_is_refused():
    with pytest.raises(ValueError, match="literal secret"):
        run_store.s3_options({**BLOCK, "secret_key": "hunter2"}, ENV)


def test_an_unset_env_var_is_named():
    with pytest.raises(ValueError, match="object_store names env var GARAGE_ACCESS_KEY_ID, which is not set"):
        run_store.s3_options(BLOCK, {"GARAGE_SECRET_ACCESS_KEY": "SK"})


class _FakePafs:
    def __init__(self):
        self.s3_kwargs = None
        self.uris = []
        outer = self

        class S3FileSystem:
            def __init__(self, **kwargs):
                outer.s3_kwargs = kwargs

        class FileSystem:
            @staticmethod
            def from_uri(uri):
                outer.uris.append(uri)
                return "from-uri-fs", "from/uri"

        self.S3FileSystem = S3FileSystem
        self.FileSystem = FileSystem


def test_filesystem_builds_an_s3_filesystem_from_the_block(monkeypatch):
    monkeypatch.setattr(run_store.os, "environ", ENV)
    pafs = _FakePafs()
    fs, path = run_store._filesystem(pafs, TracesRoot("s3://coxswain/traces", True, BLOCK))
    assert isinstance(fs, pafs.S3FileSystem)
    assert path == "coxswain/traces"
    assert pafs.s3_kwargs == run_store.s3_options(BLOCK, ENV)
    assert pafs.uris == []


def test_filesystem_without_a_block_uses_from_uri():
    pafs = _FakePafs()
    assert run_store._filesystem(pafs, TracesRoot("s3://coxswain/traces", True)) == ("from-uri-fs", "from/uri")
    assert pafs.uris == ["s3://coxswain/traces"]
    assert pafs.s3_kwargs is None


def test_the_literal_secret_wording_is_the_lakes():
    block = {**BLOCK, "secret_key": "hunter2"}
    with pytest.raises(ValueError) as lake:
        lake_config.iceberg_properties(block, ENV)
    with pytest.raises(ValueError, match=re.escape(str(lake.value))):
        run_store.s3_options(block, ENV)


def test_call_events_falls_back_to_the_loose_trace_past_an_unset_env_var(tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")  # without pyarrow, call_events raises before it reads the block
    monkeypatch.setattr(run_store, "_traces_root", lambda runs_dir: TracesRoot("s3://coxswain/traces", True, BLOCK))
    monkeypatch.delenv("GARAGE_ACCESS_KEY_ID", raising=False)
    loose = tmp_path / "call.jsonl"
    loose.write_text('{"type": "result"}\n')
    assert run_store.call_events(tmp_path, "r1", {"id": "c1", "trace": str(loose)}) == [{"type": "result"}]
    with pytest.raises(run_store.TracesUnavailable, match="object_store names env var GARAGE_ACCESS_KEY_ID, which is not set"):
        run_store.call_events(tmp_path, "r1", {"id": "c1"})
