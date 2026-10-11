"""Global settings from `~/.config/srvr/srvr.toml`; every key is optional."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import SrvrError


@dataclass(frozen=True)
class Settings:
    keep_backups: int = 5  # per service; older srvr backups are deleted after a new one
    health_timeout: int = 90  # seconds to wait for a service to become healthy
    backup_max_age_days: int = 7  # `doctor` warns when the newest backup is older
    disk_warn_percent: int = 85  # `doctor` warns above this root filesystem usage
    reboot_fail_days: int = 3  # `doctor` fails when a required reboot has waited this long
    selftest_max_age_days: int = 8  # `doctor` warns when the newest SSD self-test is older

    @classmethod
    def load(cls, path: Path) -> Settings:
        if not path.exists():
            return cls()
        try:
            raw = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise SrvrError(f"{path}: {exc}") from exc
        known = {f.name for f in fields(cls)}
        if unknown := sorted(set(raw) - known):
            raise SrvrError(f"{path}: unknown setting(s): {', '.join(unknown)}")
        for key, value in raw.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise SrvrError(f"{path}: {key} must be a positive integer")
        return cls(**raw)
