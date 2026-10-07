"""Daily store backup: pg_dump the store, upload to Garage, prune Garage backups older than 14 days.

`backup_key`, `parse_key` and `newest_key` are public and stable; the monthly restore drill imports them."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from agent_tools import lake_config, store_url

PREFIX = "store-backup/"
RETENTION = timedelta(days=14)
_STAMP = "%Y%m%dT%H%M%SZ"
_NAME = "store-"
_SUFFIX = ".dump"


class GarageClient(Protocol):
    def put(self, key: str, path: Path) -> None: ...
    def list(self, prefix: str) -> list[str]: ...
    def get(self, key: str, path: Path) -> None: ...
    def delete(self, key: str) -> None: ...
    def size(self, key: str) -> int | None:
        """Bytes in the object at `key`; None when it does not exist."""
        ...


@dataclass(frozen=True)
class GarageSettings:
    bucket: str
    endpoint: str
    access_key: str
    secret_key: str
    region: str | None = None
    path_style: bool = True


def backup_key(ts: datetime) -> str:
    """The object key for a backup taken at `ts`; `ts` must be timezone-aware and is read as UTC."""
    if ts.tzinfo is None:
        raise ValueError("backup_key needs a timezone-aware timestamp")
    return f"{PREFIX}{_NAME}{ts.astimezone(UTC).strftime(_STAMP)}{_SUFFIX}"


def parse_key(key: str) -> datetime | None:
    """The UTC timestamp `backup_key` put in `key`; None for any key it did not make."""
    if not (key.startswith(PREFIX + _NAME) and key.endswith(_SUFFIX)):
        return None
    stamp = key[len(PREFIX + _NAME) : -len(_SUFFIX)]
    try:
        parsed = datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed if backup_key(parsed) == key else None


def expired_keys(keys: Sequence[str], now: datetime) -> list[str]:
    """Backup keys strictly older than 14 days at `now`, in input order. Foreign keys are never returned."""
    cutoff = now - RETENTION
    return [key for key in keys if (ts := parse_key(key)) is not None and ts < cutoff]


def newest_key(keys: Sequence[str]) -> str | None:
    """The backup key with the latest timestamp; foreign keys are ignored."""
    stamped = [(ts, key) for key in keys if (ts := parse_key(key)) is not None]
    return max(stamped)[1] if stamped else None


def garage_settings(profile: Mapping[str, Any], env: Mapping[str, str]) -> GarageSettings:
    """The lake's bucket (from `lake_url`), endpoint and credentials (from `object_store` and the env vars it names)."""
    # Backups share the lake's bucket under PREFIX, apart from the warehouse prefix: the ticket names the lake's
    # profile as the only source, and a separate bucket would need a new profile key, which is its own change.
    config = lake_config.resolve_lake(profile, ".")
    parsed = urlparse(config.warehouse)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("the provider profile's lake_url is not an s3://bucket URL, so there is no Garage bucket")
    props = lake_config.iceberg_properties(config.object_store, env)
    missing = [name for name in ("s3.endpoint", "s3.access-key-id", "s3.secret-access-key") if name not in props]
    if missing:
        raise ValueError(f"the provider profile's object_store does not give {', '.join(missing)}")
    return GarageSettings(
        bucket=parsed.netloc,
        endpoint=props["s3.endpoint"],
        access_key=props["s3.access-key-id"],
        secret_key=props["s3.secret-access-key"],
        region=props.get("s3.region"),
        path_style=props.get("s3.force-virtual-addressing") == "false",
    )


def dump_target(url: str) -> tuple[str, str]:
    """(role, database) of the Postgres store at `url`; a store that is not Postgres cannot be pg_dumped."""
    parsed = urlparse(url)
    database = parsed.path.lstrip("/")
    if not parsed.scheme.startswith("postgres") or not parsed.username or not database:
        raise ValueError(
            f"the store is not a postgresql:// URL with a user and database ({store_url.describe_store(url)})"
        )
    return parsed.username, database


def s3_options(settings: GarageSettings) -> dict[str, Any]:
    """Keyword arguments for pyarrow's S3FileSystem."""
    endpoint = urlparse(settings.endpoint)
    has_scheme = bool(endpoint.scheme and endpoint.netloc)
    return {
        "access_key": settings.access_key,
        "secret_key": settings.secret_key,
        "endpoint_override": endpoint.netloc if has_scheme else settings.endpoint,
        "scheme": endpoint.scheme if has_scheme else "https",
        **({"region": settings.region} if settings.region else {}),
        **({} if settings.path_style else {"force_virtual_addressing": True}),
    }


class ArrowGarage:
    """Edge. A GarageClient over a pyarrow filesystem; `bucket` is the path every key lives under."""

    def __init__(self, fs: Any, bucket: str) -> None:
        self._fs, self._root = fs, bucket.rstrip("/")

    @classmethod
    def connect(cls, settings: GarageSettings) -> ArrowGarage:
        # pyarrow is the S3 access the lake already uses (lake sync needs it); no new dependency.
        try:
            from pyarrow.fs import S3FileSystem
        except ImportError as err:
            raise RuntimeError(
                "store_backup reaches Garage through pyarrow: pip install 'coxswain-tools[parquet]'"
            ) from err
        return cls(S3FileSystem(**s3_options(settings)), settings.bucket)

    def _path(self, key: str) -> str:
        return f"{self._root}/{key}"

    def put(self, key: str, path: Path) -> None:
        with path.open("rb") as src, self._fs.open_output_stream(self._path(key)) as dst:
            shutil.copyfileobj(src, dst)

    def list(self, prefix: str) -> list[str]:
        from pyarrow.fs import FileSelector, FileType

        infos = self._fs.get_file_info(
            FileSelector(self._path(prefix).rstrip("/"), recursive=True, allow_not_found=True)
        )
        return sorted(info.path[len(self._root) + 1 :] for info in infos if info.type == FileType.File)

    def get(self, key: str, path: Path) -> None:
        with self._fs.open_input_stream(self._path(key)) as src, path.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    def delete(self, key: str) -> None:
        self._fs.delete_file(self._path(key))

    def size(self, key: str) -> int | None:
        from pyarrow.fs import FileType

        info = self._fs.get_file_info(self._path(key))
        return info.size if info.type == FileType.File else None


def verify_upload(client: GarageClient, key: str) -> None:
    """Raise unless `key` is listed in Garage with a nonzero size."""
    if key not in client.list(PREFIX):
        raise RuntimeError(f"uploaded object {key} is not in the Garage listing")
    if not (client.size(key) or 0) > 0:
        raise RuntimeError(f"uploaded object {key} has zero size")


def run_backup(
    client: GarageClient, runner: Callable[..., Any], now: datetime, container: str, pg_user: str, pg_db: str
) -> str:
    """Edge. Dump, upload and verify under `backup_key(now)`, then prune expired Garage objects; the key.

    The temporary directory is removed on success and failure. Nothing is pruned unless the upload verified."""
    key = backup_key(now)
    with tempfile.TemporaryDirectory(prefix="store-backup-") as workdir:
        dump = Path(workdir) / key.removeprefix(PREFIX)
        argv = ["docker", "exec", container, "pg_dump", "-Fc", "-U", pg_user, pg_db]
        with dump.open("wb") as out:
            result = runner(argv, stdout=out, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"pg_dump exited {result.returncode}: {detail}")
        if dump.stat().st_size == 0:
            raise RuntimeError("pg_dump wrote an empty file")
        client.put(key, dump)
    verify_upload(client, key)
    for old in expired_keys(client.list(PREFIX), now):
        client.delete(old)
    return key


def load_profile(env: Mapping[str, str]) -> Mapping[str, Any]:
    """Edge. The provider profile the lake reads, chosen by `env`'s AGENT_TOOLS_PROFILE as `cox lake` chooses it."""
    from agent_tools import cli

    profile, problem = cli._lake_provider(argparse.Namespace(profile=env.get("AGENT_TOOLS_PROFILE")))
    if problem:
        raise RuntimeError(problem)
    return profile


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] = os.environ,
    client: GarageClient | None = None,
    runner: Callable[..., Any] = subprocess.run,
    now: datetime | None = None,
) -> int:
    """Edge. One backup; 0 only once the uploaded object is listed with nonzero size, else 1 with the reason on stderr."""
    parser = argparse.ArgumentParser(prog="python -m agent_tools.store_backup", description=__doc__)
    parser.add_argument("--container", required=True, help="name of the store's Postgres container")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(env)
        pg_user, pg_db = dump_target(store_url.profile_store_url(profile, "."))
        garage = client if client is not None else ArrowGarage.connect(garage_settings(profile, env))
        key = run_backup(garage, runner, now or datetime.now(UTC), args.container, pg_user, pg_db)
    except Exception as err:
        print(f"store backup failed: {err}", file=sys.stderr)
        return 1
    print(f"store backup ok: {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
