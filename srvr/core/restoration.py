"""Stage data beside each destination and journal a recoverable directory switch."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import atomic, volumes
from .errors import SrvrError
from .shell import Shell
from .ui import utc_now


@dataclass
class Directory:
    live: Path
    stage: Path
    aside: Path
    existed: bool
    member: Path | None = None
    volume: volumes.Volume | None = None
    state: str = "pending"

    @property
    def staged(self) -> Path:
        return self.stage / (self.member or self.live).name

    @property
    def failed(self) -> Path:
        return self.aside.with_name(self.aside.name.replace(".pre-restore-", ".failed-restore-"))


def exists(shell: Shell, path: Path) -> bool:
    return (
        shell.run(["test", "-e", path], sudo=True, check=False).ok
        or shell.run(["test", "-L", path], sudo=True, check=False).ok
    )


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
                    "member": str(e.member or e.live),
                    "volume": (
                        {**e.volume.record(), "filesystem": e.volume.filesystem}
                        if e.volume
                        else None
                    ),
                    "state": e.state,
                }
                for e in entries
            ],
        },
    )


def _load(
    journal: Path, directories: tuple[Path, ...], named: tuple[volumes.Volume, ...]
) -> tuple[str, list[Directory]]:
    try:
        raw: dict[str, Any] = json.loads(journal.read_text())
        phase = raw["phase"]
        by_path = {v.mountpoint: v for v in named}
        entries = []
        for e in raw["directories"]:
            live = Path(e["live"])
            volume = by_path.get(live)
            recorded = e.get("volume")
            if (volume is not None and not volumes.journal_matches(recorded, volume)) or (
                volume is None and recorded is not None
            ):
                raise ValueError("volume identity changed")
            entries.append(
                Directory(
                    live,
                    Path(e["stage"]),
                    Path(e["aside"]),
                    e["existed"],
                    Path(e.get("member", e["live"])),
                    volume,
                    e.get("state", "pending"),
                )
            )
        if phase not in ("preparing", "switching", "committed"):
            raise ValueError("unknown phase")
        if tuple(e.live for e in entries) != (*directories, *(v.mountpoint for v in named)):
            raise ValueError("registered data paths changed")
        for entry in entries:
            if (
                not isinstance(entry.existed, bool)
                or entry.stage.parent != entry.live.parent
                or not entry.stage.name.startswith(f".{entry.live.name}.srvr-restore-")
                or entry.aside.parent != entry.live.parent
                or not entry.aside.name.startswith(f"{entry.live.name}.pre-restore-")
                or entry.state
                not in (
                    "pending",
                    "evacuating",
                    "installing",
                    "discarding",
                    "recovering",
                    "recovered",
                )
                or entry.member is None
                or not entry.member.is_absolute()
                or ".." in entry.member.parts
                or entry.member.name != entry.live.name
            ):
                raise ValueError("invalid directory entry")
        return str(phase), entries
    except (ValueError, KeyError, TypeError) as exc:
        raise SrvrError(f"cannot recover restore journal {journal}: {exc}") from exc


def _contents(shell: Shell, source: Path, target: Path, volume: volumes.Volume) -> None:
    volumes.unchanged(shell, volume)
    volumes.directory_identity(shell, source)
    volumes.directory_identity(shell, target)
    children = shell.run(
        ["find", source, "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\\0"], sudo=True
    ).stdout.split("\0")
    for name in children:
        if not name:
            continue
        volumes.unchanged(shell, volume)
        if exists(shell, target / name):
            raise SrvrError("restore destination changed; refusing to overwrite recovery data")
        _move(shell, source / name, target / name)


def _attributes(shell: Shell, source: Path, target: Path) -> None:
    shell.run(["chown", "--reference", source, "--", target], sudo=True)
    shell.run(["chmod", "--reference", source, "--", target], sudo=True)
    shell.run(["touch", "--reference", source, "--", target], sudo=True)
    shell.run(["sync", "--file-system", "--", target], sudo=True)


def _stage(shell: Shell, entry: Directory, source: Path, *, snapshot: bool) -> None:
    shell.run(["mkdir", "--mode=700", "--", entry.stage], sudo=True)
    member = (entry.member or entry.live).relative_to("/")
    if snapshot:
        directory = source / member
        volumes.directory_identity(shell, directory)
        shell.run(["cp", "-a", "--no-target-directory", "--", directory, entry.staged], sudo=True)
    else:
        shell.pipeline(
            ["zstd", "--quiet", "--decompress", "--stdout", source],
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
    volumes.directory_identity(shell, entry.staged)
    if entry.volume:
        volumes.unchanged(shell, entry.volume)
        shell.run(["mkdir", "--mode=700", "--", entry.aside], sudo=True)
        _attributes(shell, entry.live, entry.aside)
    shell.run(["sync", "--file-system", "--", entry.stage], sudo=True)


def _recover_volume(
    shell: Shell, entry: Directory, journal: Path, entries: list[Directory]
) -> None:
    volume = entry.volume
    assert volume is not None
    volumes.unchanged(shell, volume)
    if entry.state in ("pending", "recovered"):
        return
    if entry.state in ("installing", "discarding"):
        entry.state = "discarding"
        _save(journal, "switching", entries)
        if not exists(shell, entry.failed):
            shell.run(["mkdir", "--mode=700", "--", entry.failed], sudo=True)
        _contents(shell, entry.live, entry.failed, volume)
    entry.state = "recovering"
    _save(journal, "switching", entries)
    _contents(shell, entry.aside, entry.live, volume)
    _attributes(shell, entry.aside, entry.live)
    entry.state = "recovered"
    _save(journal, "switching", entries)


def recover(
    shell: Shell,
    directories: tuple[Path, ...],
    journal: Path,
    *,
    named: tuple[volumes.Volume, ...] = (),
) -> None:
    if journal.is_symlink():
        raise SrvrError("restore journal is a symlink; keep the service stopped")
    if not journal.exists():
        return
    phase, entries = _load(journal, directories, named)
    failures = []
    if phase == "switching":
        for entry in reversed(entries):
            try:
                if entry.volume:
                    _recover_volume(shell, entry, journal, entries)
                elif exists(shell, entry.aside):
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
        if entry.volume:
            volumes.unchanged(shell, entry.volume)
        shell.run(["rm", "-rf", "--", entry.stage], sudo=True)
    atomic.remove(journal)


def replace(
    shell: Shell,
    directories: tuple[Path, ...],
    archive: Path,
    journal: Path,
    *,
    named: tuple[volumes.Volume, ...] = (),
    members: dict[str, Path] | None = None,
    snapshot: bool = False,
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
    for volume in named:
        directory = volume.mountpoint
        entries.append(
            Directory(
                directory,
                directory.parent / f".{directory.name}.srvr-restore-{stamp}",
                directory.with_name(f"{directory.name}.pre-restore-{stamp}"),
                True,
                (members or {})[volume.name],
                volume,
            )
        )
    for entry in entries:
        volumes.directory_identity(shell, entry.live.parent)
        if entry.existed:
            volumes.directory_identity(shell, entry.live)
        if shell.run(["mountpoint", "--quiet", "--", entry.live], sudo=True, check=False).ok:
            raise SrvrError("cannot rename or restore a mounted data root")
    _save(journal, "preparing", entries)
    try:
        for entry in entries:
            _stage(shell, entry, archive, snapshot=snapshot)
        _save(journal, "switching", entries)
        for entry in entries:
            if entry.volume:
                entry.state = "evacuating"
                _save(journal, "switching", entries)
                _contents(shell, entry.live, entry.aside, entry.volume)
                entry.state = "installing"
                _save(journal, "switching", entries)
                _contents(shell, entry.staged, entry.live, entry.volume)
                _attributes(shell, entry.staged, entry.live)
            else:
                if entry.existed:
                    _move(shell, entry.live, entry.aside)
                _move(shell, entry.staged, entry.live)
        _save(journal, "committed", entries)
    except BaseException:
        recover(shell, directories, journal, named=named)
        raise
    recover(shell, directories, journal, named=named)
    return [e.aside for e in entries if e.existed]
