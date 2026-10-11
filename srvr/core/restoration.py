"""Stage data beside each destination and journal a recoverable directory switch."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import atomic
from .errors import SrvrError
from .shell import Shell
from .ui import utc_now


@dataclass(frozen=True)
class Directory:
    live: Path
    stage: Path
    aside: Path
    existed: bool

    @property
    def staged(self) -> Path:
        return self.stage / self.live.name

    @property
    def failed(self) -> Path:
        return self.aside.with_name(self.aside.name.replace(".pre-restore-", ".failed-restore-"))


def exists(shell: Shell, path: Path) -> bool:
    return shell.run(["test", "-e", path], sudo=True, check=False).ok


def _move(shell: Shell, source: Path, target: Path) -> None:
    shell.run(["mv", "--no-target-directory", "--", source, target], sudo=True)
    shell.run(["sync", "--file-system", "--", target.parent], sudo=True)


def _save(journal: Path, phase: str, entries: list[Directory]) -> None:
    atomic.write_json(
        journal,
        {
            "phase": phase,
            "directories": [
                {
                    "live": str(e.live),
                    "stage": str(e.stage),
                    "aside": str(e.aside),
                    "existed": e.existed,
                }
                for e in entries
            ],
        },
    )


def _load(journal: Path, directories: tuple[Path, ...]) -> tuple[str, list[Directory]]:
    try:
        raw: dict[str, Any] = json.loads(journal.read_text())
        phase = raw["phase"]
        entries = [
            Directory(Path(e["live"]), Path(e["stage"]), Path(e["aside"]), e["existed"])
            for e in raw["directories"]
        ]
        if phase not in ("preparing", "switching", "committed"):
            raise ValueError("unknown phase")
        if tuple(e.live for e in entries) != directories:
            raise ValueError("registered data paths changed")
        for entry in entries:
            if (
                not isinstance(entry.existed, bool)
                or entry.stage.parent != entry.live.parent
                or not entry.stage.name.startswith(f".{entry.live.name}.srvr-restore-")
                or entry.aside.parent != entry.live.parent
                or not entry.aside.name.startswith(f"{entry.live.name}.pre-restore-")
            ):
                raise ValueError("invalid directory entry")
        return str(phase), entries
    except (ValueError, KeyError, TypeError) as exc:
        raise SrvrError(f"cannot recover restore journal {journal}: {exc}") from exc


def recover(shell: Shell, directories: tuple[Path, ...], journal: Path) -> None:
    if not journal.exists():
        return
    phase, entries = _load(journal, directories)
    failures = []
    if phase == "switching":
        for entry in reversed(entries):
            try:
                if exists(shell, entry.aside):
                    if exists(shell, entry.live):
                        _move(shell, entry.live, entry.failed)
                    _move(shell, entry.aside, entry.live)
                elif (
                    not entry.existed
                    and not exists(shell, entry.staged)
                    and exists(shell, entry.live)
                ):
                    _move(shell, entry.live, entry.failed)
            except (SrvrError, OSError) as exc:
                failures.append(f"{entry.live}: {exc}")
    if failures:
        raise SrvrError(
            f"restore recovery incomplete; keep the service stopped; journal {journal}: "
            + "; ".join(failures)
        )
    for entry in entries:
        shell.run(["rm", "-rf", "--", entry.stage], sudo=True)
    atomic.remove(journal)


def replace(
    shell: Shell, directories: tuple[Path, ...], archive: Path, journal: Path
) -> list[Path]:
    stamp = f"{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
    entries = [
        Directory(
            directory,
            directory.parent / f".{directory.name}.srvr-restore-{stamp}",
            directory.with_name(f"{directory.name}.pre-restore-{stamp}"),
            exists(shell, directory),
        )
        for directory in directories
    ]
    _save(journal, "preparing", entries)
    try:
        for entry in entries:
            shell.run(["mkdir", "--mode=700", "--", entry.stage], sudo=True)
            member = entry.live.relative_to("/")
            shell.pipeline(
                ["zstd", "--quiet", "--decompress", "--stdout", archive],
                [
                    "tar",
                    "--extract",
                    "--numeric-owner",
                    "--same-permissions",
                    "--strip-components",
                    str(len(member.parts) - 1),
                    "--directory",
                    entry.stage,
                    "--",
                    member,
                ],
                sudo_second=True,
            )
            shell.run(["test", "-d", entry.staged], sudo=True)
            shell.run(["sync", "--file-system", "--", entry.stage], sudo=True)
        _save(journal, "switching", entries)
        for entry in entries:
            if entry.existed:
                _move(shell, entry.live, entry.aside)
            _move(shell, entry.staged, entry.live)
        _save(journal, "committed", entries)
    except BaseException:
        recover(shell, directories, journal)
        raise
    recover(shell, directories, journal)
    return [e.aside for e in entries if e.existed]
