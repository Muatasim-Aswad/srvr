"""Service data backups: a zstd-compressed tar of the data directories, taken while stopped.

Archives live in `~/.local/share/srvr/backups/<service>/<UTC time>-<reason>.tar.zst`, each with a
`.json` sidecar. tar runs as root (data may belong to container users) and keeps numeric owners,
so a restore reproduces ownership and modes exactly."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import atomic, deploys, restoration
from .errors import SrvrError
from .paths import Paths, ensure_private_dir, keep_home_owner
from .registry import Service
from .shell import Shell
from .ui import iso, utc_now

SUFFIX = ".tar.zst"


@dataclass(frozen=True)
class Backup:
    path: Path
    meta: dict[str, Any]

    @property
    def name(self) -> str:
        return self.path.name.removesuffix(SUFFIX)

    @property
    def size(self) -> int:
        return self.path.stat().st_size


def _meta_path(archive: Path) -> Path:
    return archive.with_name(archive.name.removesuffix(SUFFIX) + ".json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_backups(paths: Paths, service: str) -> list[Backup]:
    """Newest first: creation time for new archives, modification time for older ones."""
    directory = paths.service_backups(service)
    if not directory.is_dir():
        return []
    backups = []
    for archive in sorted(directory.glob(f"*{SUFFIX}"), reverse=True):
        meta_file = _meta_path(archive)
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        backups.append(Backup(archive, meta))
    backups.sort(
        key=lambda b: (b.meta.get("created_ns", b.path.stat().st_mtime_ns), b.name), reverse=True
    )
    return backups


def find(paths: Paths, service: str, name: str) -> Backup:
    backups = list_backups(paths, service)
    if not backups:
        raise SrvrError(f"{service}: no backups")
    if name == "latest":
        return backups[0]
    for backup in backups:
        if backup.name == name:
            return backup
    raise SrvrError(f"{service}: no backup named {name!r} (see `srvr backups {service}`)")


def create(shell: Shell, paths: Paths, service: Service, *, reason: str, version: str) -> Backup:
    """Archive the service's data directories. The caller must have stopped the service."""
    if not service.data:
        raise SrvrError(f"{service.name}: no data directories registered")
    missing = [str(d) for d in service.data if not d.exists()]
    if missing:
        raise SrvrError(f"{service.name}: data directory missing: {', '.join(missing)}")

    now = utc_now()
    directory = ensure_private_dir(paths.service_backups(service.name))
    name = f"{now:%Y%m%dT%H%M%SZ}-{reason}"
    archive = directory / f"{name}{SUFFIX}"
    counter = 0
    while archive.exists() or _meta_path(archive).exists():
        counter += 1
        name = f"{now:%Y%m%dT%H%M%SZ}-{reason}-{counter:06d}"
        archive = directory / f"{name}{SUFFIX}"
    partial = directory / f".{name}{SUFFIX}.partial"
    members = [str(d.relative_to("/")) for d in service.data]

    old_umask = os.umask(0o077)
    try:
        partial.unlink(missing_ok=True)
        shell.pipeline(
            ["tar", "--create", "--numeric-owner", "--directory", "/", "--", *members],
            ["zstd", "--quiet", "-T0", "-o", str(partial)],
            sudo_first=True,
        )
        shell.run(["zstd", "--quiet", "--test", str(partial)])
        meta = {
            "service": service.name,
            "time": iso(now),
            "created_ns": time.time_ns(),
            "reason": reason,
            "version": version,
            "data": [str(d) for d in service.data],
            "sha256": _sha256(partial),
        }
        keep_home_owner(partial)
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        atomic.write_json(_meta_path(archive), meta)
        partial.rename(archive)
        atomic.sync_directory(directory)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)
    return Backup(archive, meta)


def verify(shell: Shell, backup: Backup) -> None:
    expected = backup.meta.get("sha256")
    if expected and _sha256(backup.path) != expected:
        raise SrvrError(f"{backup.name}: checksum mismatch; the archive is damaged")
    shell.run(["zstd", "--quiet", "--test", str(backup.path)])


def prune(paths: Paths, service: str, *, keep: int) -> list[Backup]:
    """Keep the newest `keep` and the recorded rollback backup; return removed archives."""
    record = deploys.load_record(paths, service)
    protected = record.backup if record else None
    removed = [b for b in list_backups(paths, service)[keep:] if b.name != protected]
    for backup in removed:
        backup.path.unlink()
        _meta_path(backup.path).unlink(missing_ok=True)
    return removed


def restore(shell: Shell, service: Service, backup: Backup) -> list[Path]:
    """Replace the data directories with the backup. The caller must have stopped the service.

    Current data is moved aside (never deleted) to `<dir>.pre-restore-<time>`; those paths are
    returned. If extraction fails, the moved-aside data is put back."""
    restoration.recover(shell, service.data, backup.path.parent / ".restore.json")
    if not service.data:
        raise SrvrError(f"{service.name}: no data directories registered")
    recorded = backup.meta.get("data")
    if recorded is not None and recorded != [str(d) for d in service.data]:
        raise SrvrError(f"{backup.name}: its data paths {recorded} differ from the registry's")
    verify(shell, backup)

    return restoration.replace(
        shell, service.data, backup.path, backup.path.parent / ".restore.json"
    )
