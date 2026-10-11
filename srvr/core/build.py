"""Identify the installed artifact or source checkout without accessing service state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from srvr import __version__
from srvr.core.errors import SrvrError
from srvr.core.shell import Shell


def info(shell: Shell, root: Path | None = None) -> dict[str, str]:
    root = root or Path(__file__).resolve().parents[2]
    stamp = root / "srvr/build.json"
    if stamp.exists() and not (root / ".git").exists():
        record: dict[str, str] = json.loads(stamp.read_text())
        return record
    record = {"version": __version__, "revision": "dev", "commit_time": "", "build_time": ""}
    if (root / ".git").exists():
        try:
            result = shell.run(["git", "show", "-s", "--format=%H%n%cI", "HEAD"], cwd=root)
            record["revision"], record["commit_time"] = result.stdout.strip().splitlines()
            record["commit_time"] = (
                datetime.fromisoformat(record["commit_time"])
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            if shell.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root).stdout:
                record["revision"] = "dev"
        except SrvrError:
            pass
    return record
