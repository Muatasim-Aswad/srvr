"""Filesystem locations used by srvr, following the XDG base-directory layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else Path.home() / default


@dataclass(frozen=True)
class Paths:
    config: Path  # settings and the service registry
    state: Path  # deploy records, history, locks
    data: Path  # backups

    @classmethod
    def from_env(cls) -> Paths:
        return cls(
            config=_xdg("XDG_CONFIG_HOME", ".config") / "srvr",
            state=_xdg("XDG_STATE_HOME", ".local/state") / "srvr",
            data=_xdg("XDG_DATA_HOME", ".local/share") / "srvr",
        )

    @property
    def settings_file(self) -> Path:
        return self.config / "srvr.toml"

    @property
    def services_dir(self) -> Path:
        return self.config / "services"

    def service_state(self, name: str) -> Path:
        return self.state / name

    def service_backups(self, name: str) -> Path:
        return self.data / "backups" / name


def keep_home_owner(path: Path) -> None:
    """Under root (unattended backups preserve the administrator's HOME), give new files to the
    home directory's owner so later runs as that user can still write it."""
    if os.geteuid() != 0:
        return
    home = Path.home().stat()
    os.chown(path, home.st_uid, home.st_gid)


def ensure_private_dir(path: Path) -> Path:
    """Create `path` (and parents) with owner-only permissions on the leaf."""
    created = [p for p in (path, *path.parents) if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    for directory in created:
        keep_home_owner(directory)
    return path
