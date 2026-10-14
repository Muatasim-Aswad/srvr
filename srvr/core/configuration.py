"""Detect changed host configuration without retaining or displaying secret values."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import SrvrError
from .registry import Service


def files(service: Service) -> tuple[Path, ...]:
    files = {*service.compose_files, *service.config_files, service.workdir / ".env"}
    files.update(p for p in (service.env_file,) if p is not None)
    return tuple(sorted(files))


def revision(service: Service) -> str:
    paths = {*files(service), service.workdir, *service.data}
    if service.source is not None:
        paths.add(service.source)
    digest = hashlib.sha256()
    for file in sorted(paths):
        digest.update(str(file).encode() + b"\0")
        digest.update(str(file.resolve()).encode() + b"\0")
        if file.exists():
            stat = file.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_uid, stat.st_gid)
            digest.update(repr(identity).encode() + b"\0")
            digest.update(b"directory" if file.is_dir() else file.read_bytes())
        else:
            digest.update(b"missing")
        digest.update(b"\0")
    return digest.hexdigest()


def validate(service: Service, expected: str) -> None:
    if revision(service) != expected:
        raise SrvrError(f"{service.name}: configuration changed; rerun the command")
