import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_tools.store_backup import (
    ArrowGarage,
    GarageSettings,
    backup_key,
    dump_target,
    expired_keys,
    garage_settings,
    load_profile,
    main,
    newest_key,
    parse_key,
    run_backup,
    s3_options,
)

NOW = datetime(2026, 10, 7, 3, 15, 0, tzinfo=UTC)
K_NOW = "store-backup/store-20261007T031500Z.dump"


def key_at(age: timedelta) -> str:
    return backup_key(NOW - age)


class FakeGarage:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.deleted: list[str] = []
        self.drop_puts = False
        self.fail_put = False

    def put(self, key: str, path: Path) -> None:
        if self.fail_put:
            raise OSError("garage unreachable")
        if not self.drop_puts:
            self.objects[key] = path.read_bytes()

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str, path: Path) -> None:
        path.write_bytes(self.objects[key])

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        del self.objects[key]

    def size(self, key: str) -> int | None:
        return len(self.objects[key]) if key in self.objects else None


class FakeRunner:
    def __init__(self, payload: bytes = b"PGDMP", returncode: int = 0) -> None:
        self.payload, self.returncode = payload, returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, stdout, stderr, check):
        self.calls.append(list(argv))
        stdout.write(self.payload)
        return SimpleNamespace(returncode=self.returncode, stderr=b"no such container" if self.returncode else b"")


@pytest.fixture
def workroot(tmp_path, monkeypatch):
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def test_backup_key():
    assert backup_key(NOW) == K_NOW
    assert backup_key(datetime(2026, 10, 7, 5, 15, 0, tzinfo=timezone(timedelta(hours=2)))) == K_NOW
    with pytest.raises(ValueError):
        backup_key(datetime(2026, 10, 7, 3, 15, 0))  # noqa: DTZ001


def test_parse_key():
    assert parse_key(K_NOW) == NOW
    for foreign in ("other/x.dump", "store-backup/readme.txt", "store-backup/store-garbage.dump"):
        assert parse_key(foreign) is None


def test_expired_keys_keeps_exactly_14_days_and_foreign_keys():
    keys = [
        key_at(timedelta(days=13, hours=23)),
        key_at(timedelta(days=14)),
        key_at(timedelta(days=15)),
        "store-backup/notes.txt",
    ]
    assert expired_keys(keys, NOW) == [keys[2]]


def test_newest_key():
    assert newest_key([key_at(timedelta(days=3)), K_NOW, key_at(timedelta(days=1)), "store-backup/zzz.txt"]) == K_NOW
    assert newest_key(["other/x.dump"]) is None


OBJECT_STORE = {"endpoint": "http://garage.example:3900", "access_key_env": "G_KEY", "secret_key_env": "G_SECRET"}
PROFILE = {
    "lake_url": "s3://lake-bucket/warehouse",
    "object_store": OBJECT_STORE,
    "storage_url": "postgresql://cox:pw@store-db:5432/coxstore",
}
ENV = {"G_KEY": "key-id", "G_SECRET": "key-secret"}


def test_garage_settings_from_profile_and_env():
    assert garage_settings(PROFILE, ENV) == GarageSettings(
        "lake-bucket", "http://garage.example:3900", "key-id", "key-secret"
    )
    with pytest.raises(ValueError, match="no Garage bucket"):
        garage_settings({}, ENV)
    with pytest.raises(ValueError, match="G_KEY"):
        garage_settings(PROFILE, {"G_SECRET": "s"})


def test_s3_options_split_the_endpoint():
    assert s3_options(garage_settings(PROFILE, ENV)) == {
        "access_key": "key-id",
        "secret_key": "key-secret",
        "endpoint_override": "garage.example:3900",
        "scheme": "http",
    }


def test_dump_target_reads_the_store_url():
    assert dump_target(PROFILE["storage_url"]) == ("cox", "coxstore")
    with pytest.raises(ValueError, match="store: sqlite"):
        dump_target("sqlite:///cox.db")


def test_run_backup_uploads_then_prunes_garage_only(workroot, tmp_path):
    sentinel = tmp_path / "local-old.dump"
    sentinel.write_bytes(b"keep me")
    old, fresh = key_at(timedelta(days=20)), key_at(timedelta(days=2))
    garage = FakeGarage({old: b"x", fresh: b"y"})
    runner = FakeRunner(b"PGDMP-data")

    assert run_backup(garage, runner, NOW, "store-db", "cox", "coxstore") == K_NOW
    assert runner.calls == [["docker", "exec", "store-db", "pg_dump", "-Fc", "-U", "cox", "coxstore"]]
    assert garage.objects == {fresh: b"y", K_NOW: b"PGDMP-data"}
    assert sentinel.read_bytes() == b"keep me"
    assert list(workroot.iterdir()) == []


def test_run_backup_cleans_up_and_keeps_old_backups_on_failure(workroot):
    old = key_at(timedelta(days=20))
    for garage, runner, error in (
        (FakeGarage({old: b"x"}), FakeRunner(returncode=1), "pg_dump exited 1: no such container"),
        (FakeGarage({old: b"x"}), FakeRunner(b""), "empty"),
        (FakeGarage({old: b"x"}), FakeRunner(), "garage unreachable"),
        (FakeGarage({old: b"x"}), FakeRunner(), "not in the Garage listing"),
    ):
        garage.fail_put = error == "garage unreachable"
        garage.drop_puts = error == "not in the Garage listing"
        with pytest.raises((RuntimeError, OSError), match=error):
            run_backup(garage, runner, NOW, "store-db", "cox", "coxstore")
        assert garage.deleted == []
        assert list(workroot.iterdir()) == []


def test_run_backup_refuses_zero_size_object_before_pruning(workroot):
    old = key_at(timedelta(days=20))
    garage = FakeGarage({old: b"x"})
    garage.size = lambda key: 0
    with pytest.raises(RuntimeError, match="zero size"):
        run_backup(garage, FakeRunner(), NOW, "store-db", "cox", "coxstore")
    assert garage.deleted == []


def test_arrow_garage_over_a_local_filesystem(workroot, tmp_path):
    fs = pytest.importorskip("pyarrow.fs")
    bucket = tmp_path / "bucket"
    (bucket / "store-backup").mkdir(parents=True)
    old = key_at(timedelta(days=20))
    (bucket / old).write_bytes(b"old")
    (bucket / "store-backup/notes.txt").write_bytes(b"n")
    garage = ArrowGarage(fs.LocalFileSystem(), str(bucket))

    assert run_backup(garage, FakeRunner(b"PGDMP-data"), NOW, "store-db", "cox", "coxstore") == K_NOW
    assert garage.list("store-backup/") == sorted([K_NOW, "store-backup/notes.txt"])
    assert (garage.size(K_NOW), garage.size(old)) == (10, None)
    garage.get(K_NOW, tmp_path / "back.dump")
    assert (tmp_path / "back.dump").read_bytes() == b"PGDMP-data"


def write_profiles(tmp_path: Path, provider: str) -> dict[str, str]:
    (tmp_path / "provider.yaml").write_text(provider, encoding="utf-8")
    (tmp_path / "profile.yaml").write_text(f"provider_profile: {tmp_path / 'provider.yaml'}\n", encoding="utf-8")
    return {**ENV, "AGENT_TOOLS_PROFILE": str(tmp_path / "profile.yaml")}


PROVIDER_YAML = """\
lake_url: s3://lake-bucket/warehouse
storage_url: postgresql://cox:pw@store-db:5432/coxstore
object_store:
  endpoint: http://garage.example:3900
  access_key_env: G_KEY
  secret_key_env: G_SECRET
"""


def test_load_profile_follows_the_injected_env(tmp_path):
    assert load_profile(write_profiles(tmp_path, PROVIDER_YAML)) == PROFILE
    with pytest.raises(RuntimeError, match="no profile at"):
        load_profile({"AGENT_TOOLS_PROFILE": str(tmp_path / "missing.yaml")})


def test_main_success(workroot, tmp_path, capsys):
    garage, runner = FakeGarage(), FakeRunner()
    code = main(
        ["--container", "store-db"], env=write_profiles(tmp_path, PROVIDER_YAML), client=garage, runner=runner, now=NOW
    )
    assert (code, capsys.readouterr().out) == (0, f"store backup ok: {K_NOW}\n")
    assert runner.calls[0][-3:] == ["-U", "cox", "coxstore"]
    assert K_NOW in garage.objects


def test_main_fails_nonzero(workroot, tmp_path, capsys):
    env = write_profiles(tmp_path, PROVIDER_YAML)
    assert (
        main(["--container", "store-db"], env=env, client=FakeGarage(), runner=FakeRunner(returncode=2), now=NOW) == 1
    )
    assert "store backup failed: pg_dump exited 2" in capsys.readouterr().err
    sqlite_env = write_profiles(tmp_path, "storage_url: sqlite:///cox.db\n")
    assert main(["--container", "store-db"], env=sqlite_env, client=FakeGarage(), runner=FakeRunner(), now=NOW) == 1
    assert "not a postgresql:// URL" in capsys.readouterr().err
