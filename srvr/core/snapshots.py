"""Plain copies of service data for an off-server backup tool (restic) to pick up.

`<dest>/<service>/` mirrors each data directory at its absolute path, so a restore copies it
straight back. Unlike `backups`, nothing is compressed: the backup tool deduplicates unchanged
files. A copy is built beside the previous one and swapped in only when complete, so a failure
leaves the last good copy in place. Copies run as root (data may belong to container users) and
keep owners and modes."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import atomic, restoration, volumes
from .errors import SrvrError
from .registry import Service
from .shell import Shell


def location(dest: Path, service: Service) -> Path:
    return dest / service.name


@dataclass(frozen=True)
class Snapshot:
    path: Path
    meta: dict[str, Any]
    filesystem: str
    digest: str


def _digest(shell: Shell, source: Path) -> str:
    metadata = shell.run(
        ["find", source, "-printf", "%P\\t%y:%m:%U:%G:%s:%T@:%l\\0"], sudo=True
    ).stdout
    contents = shell.run(
        ["find", source, "-type", "f", "-exec", "sha256sum", "--zero", "--", "{}", "+"],
        sudo=True,
    ).stdout
    value = hashlib.sha256()
    for entries in (metadata, contents):
        value.update("\0".join(sorted(entries.split("\0"))).encode() + b"\0")
    return value.hexdigest()


def read(shell: Shell, service: Service, source: Path) -> Snapshot:
    source = source.absolute()
    fingerprint = volumes.directory_identity(shell, source)
    manifest = source / ".srvr-data.json"
    if _exists(shell, manifest):
        if shell.run(["test", "-L", manifest], sudo=True, check=False).ok:
            raise SrvrError("snapshot manifest must not be a symlink")
        try:
            meta = json.loads(shell.run(["cat", "--", manifest], sudo=True).stdout)
        except json.JSONDecodeError as exc:
            raise SrvrError("unreadable snapshot manifest") from exc
        if not isinstance(meta, dict) or meta.get("service") != service.name:
            raise SrvrError("snapshot belongs to a different service")
    elif service.data_volumes:
        raise SrvrError("named-volume snapshots require their .srvr-data.json manifest")
    else:
        meta = {"data": [str(d) for d in service.data], "data_volumes": []}
    if meta.get("data") != [str(d) for d in service.data]:
        raise SrvrError("snapshot data paths differ from the registry")
    return Snapshot(source, meta, fingerprint, _digest(shell, source))


def restore(shell: Shell, service: Service, snapshot: Snapshot, journal: Path) -> list[Path]:
    named = volumes.inspect(shell, service)
    restoration.recover(shell, service.data, journal, named=named)
    if not service.has_data:
        raise SrvrError(f"{service.name}: no data registered")
    if read(shell, service, snapshot.path) != snapshot:
        raise SrvrError("snapshot changed; rerun restore")
    members = volumes.archive_members(snapshot.meta.get("data_volumes", []), named)
    if any(
        snapshot.path.is_relative_to(d) or d.is_relative_to(snapshot.path) for d in service.data
    ):
        raise SrvrError("restore snapshot must be outside live data")
    if any(snapshot.path.is_relative_to(v.mountpoint.parents[2]) for v in named):
        raise SrvrError("restore snapshot must be outside Docker-managed storage")
    volumes.revalidate(shell, service, named)
    return restoration.replace(
        shell,
        service.data,
        snapshot.path,
        journal,
        named=named,
        members=members,
        snapshot=True,
    )


def _exists(shell: Shell, path: Path) -> bool:
    return shell.run(["test", "-e", path], sudo=True, check=False).ok


def _move(shell: Shell, source: Path, target: Path) -> None:
    shell.run(["mv", "--no-target-directory", "--", source, target], sudo=True)
    shell.run(["sync", "--file-system", "--", target.parent], sudo=True)


def _manifest(
    shell: Shell, service: Service, named: tuple[volumes.Volume, ...], partial: Path
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        file = Path(temporary) / "data.json"
        atomic.write_json(
            file,
            {
                "service": service.name,
                "data": [str(d) for d in service.data],
                "data_volumes": [v.record() for v in named],
            },
        )
        shell.run(["install", "--mode=600", "--", file, partial / ".srvr-data.json"], sudo=True)


def _sources(
    shell: Shell, service: Service, dest: Path
) -> tuple[tuple[Path, ...], tuple[volumes.Volume, ...]]:
    if not service.has_data:
        raise SrvrError(f"{service.name}: no data registered")
    named = volumes.inspect(shell, service)
    volumes.destination(shell, dest)
    missing = [str(d) for d in service.data if not d.exists()]
    if missing:
        raise SrvrError(f"{service.name}: data directory missing: {', '.join(missing)}")
    sources = (*service.data, *(v.mountpoint for v in named))
    for source in sources:
        if dest == source or dest.is_relative_to(source) or source.is_relative_to(dest):
            raise SrvrError("snapshot destination overlaps registered data")
    if any(dest.is_relative_to(v.mountpoint.parents[2]) for v in named):
        raise SrvrError("snapshot destination overlaps Docker-managed storage")
    return sources, named


def copy(shell: Shell, service: Service, dest: Path) -> Path:
    """Replace the service's copy under `dest`. The caller must have stopped the service."""
    sources, named = _sources(shell, service, dest)

    final = location(dest, service)
    partial = dest / f".{service.name}.partial"
    previous = dest / f".{service.name}.previous"
    for path in (final, partial, previous):
        if _exists(shell, path):
            volumes.directory_identity(shell, path)
    if _exists(shell, previous):
        if _exists(shell, final):
            shell.run(["rm", "-rf", "--", previous], sudo=True)
        else:
            _move(shell, previous, final)
    shell.run(["rm", "-rf", "--", partial], sudo=True)
    try:
        shell.run(["mkdir", "-p", "--", partial], sudo=True)
        shell.run(["chmod", "700", "--", dest, partial], sudo=True)
        for directory in sources:
            target = partial / directory.relative_to("/")
            shell.run(["mkdir", "-p", "--", target.parent], sudo=True)
            shell.run(["cp", "-a", "--no-target-directory", "--", directory, target], sudo=True)
        if named:
            _manifest(shell, service, named, partial)
        volumes.revalidate(shell, service, named)
        shell.run(["sync", "--file-system", "--", partial], sudo=True)
        if _exists(shell, final):
            _move(shell, final, previous)
        _move(shell, partial, final)
    except BaseException:
        if _exists(shell, previous):
            if _exists(shell, final):
                shell.run(["rm", "-rf", "--", final], sudo=True)
            _move(shell, previous, final)
        shell.run(["rm", "-rf", "--", partial], sudo=True, check=False)
        raise
    shell.run(["rm", "-rf", "--", previous], sudo=True, check=False)
    return final
