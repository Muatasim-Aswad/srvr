"""Per-service lock so two srvr runs never change the same service at once."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import SrvrError
from .paths import Paths, ensure_private_dir, keep_home_owner


@contextmanager
def service_lock(paths: Paths, service: str, *, allow_restore: bool = False) -> Iterator[None]:
    file = ensure_private_dir(paths.service_state(service)) / "lock"
    with file.open("w") as handle:
        keep_home_owner(file)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SrvrError(f"{service}: another srvr command is changing it right now") from exc
        try:
            if not allow_restore and (paths.service_backups(service) / ".restore.json").exists():
                raise SrvrError(
                    f"{service}: unfinished restore; recover with srvr restore {service}"
                )
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
