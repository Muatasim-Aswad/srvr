"""Durable file replacement for private state and registered configuration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .paths import ensure_private_dir, keep_home_owner


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_text(path: Path, text: str, *, private: bool = False) -> None:
    path = path.resolve()
    if private:
        ensure_private_dir(path.parent)
    original = path.stat() if path.exists() else None
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            mode = 0o600 if private else (original.st_mode & 0o777 if original else 0o644)
            os.fchmod(handle.fileno(), mode)
            if private:
                keep_home_owner(temporary)
            elif original is not None and os.geteuid() == 0:
                os.fchown(handle.fileno(), original.st_uid, original.st_gid)
            os.fsync(handle.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict[str, Any]) -> None:
    write_text(path, json.dumps(value, indent=2) + "\n", private=True)


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)
    sync_directory(path.parent)
