"""Detect changed host configuration without retaining or displaying secret values."""

from __future__ import annotations

import hashlib

from .errors import SrvrError
from .registry import Service


def revision(service: Service) -> str:
    files = {*service.compose_files, *service.config_files, service.workdir / ".env"}
    files.update(p for p in (service.source, service.env_file) if p is not None)
    digest = hashlib.sha256()
    for file in sorted(files):
        digest.update(str(file).encode() + b"\0")
        digest.update(b"present\0" + file.read_bytes() if file.exists() else b"missing\0")
        digest.update(b"\0")
    return digest.hexdigest()


def validate(service: Service, expected: str) -> None:
    if revision(service) != expected:
        raise SrvrError(f"{service.name}: configuration changed; rerun the command")
