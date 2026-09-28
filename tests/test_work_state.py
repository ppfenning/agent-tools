from agent_tools import cli, work_state
from agent_tools.work_state import work_state_mode, work_state_mode_at


def _refuse_probe(url):
    raise AssertionError("the probe must not run for this case")


def test_the_exact_string_store_selects_store_mode(monkeypatch):
    # No storage_url: a non-Postgres store is never probed, so explicit "store" is trusted.
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    assert work_state_mode({"work_state": "store"}) == "store"


def test_the_string_files_selects_files_mode():
    assert work_state_mode({"work_state": "files"}) == "files"


def test_a_missing_key_selects_files_mode():
    assert work_state_mode({}) == "files"


def test_a_null_selects_files_mode():
    assert work_state_mode({"work_state": None}) == "files"


def test_an_unknown_value_selects_files_mode():
    assert work_state_mode({"work_state": "Store"}) == "files"


def test_the_edge_returns_the_mode_of_the_profile_it_reads(monkeypatch):
    monkeypatch.setattr(work_state, "read_provider_profile", lambda path: {"work_state": "store"})
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    assert work_state_mode_at("any/path.yaml") == "store"


def test_the_edge_yields_files_when_the_profile_reads_as_empty(monkeypatch):
    monkeypatch.setattr(work_state, "read_provider_profile", lambda path: {})
    assert work_state_mode_at("any/path.yaml") == "files"


def test_explicit_files_with_a_reachable_postgres_store_stays_files_and_never_probes(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    profile = {"work_state": "files", "storage_url": "postgresql://host/db"}
    assert work_state_mode(profile) == "files"
    assert work_state.resolve(profile) == ("files", "work state: files")


def test_no_key_with_a_postgres_store_that_answers_resolves_store(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", lambda url: True)
    profile = {"storage_url": "postgresql://host/db"}
    assert work_state.resolve(profile) == ("store", "work state: store")


def test_no_key_with_a_postgres_store_that_does_not_answer_resolves_files_unreachable(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", lambda url: False)
    profile = {"storage_url": "postgresql://host/db"}
    assert work_state.resolve(profile) == ("files", "work state: files (store unreachable)")


def test_no_key_with_a_sqlite_storage_url_resolves_files_and_never_probes(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    assert work_state_mode({"storage_url": "sqlite:///cox.db"}) == "files"


def test_no_key_with_no_storage_url_resolves_files_and_never_probes(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    assert work_state_mode({}) == "files"


def test_explicit_store_with_an_unreachable_store_falls_back_to_files(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", lambda url: False)
    profile = {"work_state": "store", "storage_url": "postgresql://host/db"}
    assert work_state_mode(profile) == "files"
    assert work_state.resolve(profile) == ("files", "work state: files (store unreachable)")


def test_a_typo_value_resolves_files_and_never_probes(monkeypatch):
    monkeypatch.setattr(work_state, "_probe_store", _refuse_probe)
    assert work_state_mode({"work_state": "Store", "storage_url": "postgresql://host/db"}) == "files"


class _ConnWhoseCloseFails:
    def execute(self, sql):
        return None

    def close(self):
        raise OSError("close failed")


def test_the_probe_adds_a_connect_timeout_and_reads_a_failing_close_as_no_answer(monkeypatch):
    opened = []
    monkeypatch.setattr(work_state.store_dialect, "connect_readonly_url", lambda url: opened.append(url) or _ConnWhoseCloseFails())
    assert work_state._probe_store("postgresql://host/db") is False
    assert opened == ["postgresql://host/db?connect_timeout=3"]


def test_route_context_turns_a_failing_provider_read_into_its_unavailable_line(monkeypatch, capsys):
    def unreadable(_a):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(cli, "_lake_provider", unreadable)
    assert cli.main(["route", "context", "--profile", "/nonexistent/profile.yaml"]) == 0
    assert capsys.readouterr().out.startswith("routing: context unavailable (UnicodeDecodeError")
