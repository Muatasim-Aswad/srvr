"""Plain copies of service data for an off-server backup tool (restic) to pick up.

`<dest>/<service>/` mirrors each data directory at its absolute path, so a restore copies it
straight back. Unlike `backups`, nothing is compressed: the backup tool deduplicates unchanged
files. A copy is built beside the previous one and swapped in only when complete, so a failure
leaves the last good copy in place. Copies run as root (data may belong to container users) and
keep owners and modes."""

from __future__ import annotations

from pathlib import Path

from .errors import SrvrError
from .registry import Service
from .shell import Shell


def location(dest: Path, service: Service) -> Path:
    return dest / service.name


def _exists(shell: Shell, path: Path) -> bool:
    return shell.run(["test", "-e", path], sudo=True, check=False).ok


def _move(shell: Shell, source: Path, target: Path) -> None:
    shell.run(["mv", "--no-target-directory", "--", source, target], sudo=True)
    shell.run(["sync", "--file-system", "--", target.parent], sudo=True)


def copy(shell: Shell, service: Service, dest: Path) -> Path:
    """Replace the service's copy under `dest`. The caller must have stopped the service."""
    if not service.data:
        raise SrvrError(f"{service.name}: no data directories registered")
    missing = [str(d) for d in service.data if not d.exists()]
    if missing:
        raise SrvrError(f"{service.name}: data directory missing: {', '.join(missing)}")

    final = location(dest, service)
    partial = dest / f".{service.name}.partial"
    previous = dest / f".{service.name}.previous"
    if _exists(shell, previous):
        if _exists(shell, final):
            shell.run(["rm", "-rf", "--", previous], sudo=True)
        else:
            _move(shell, previous, final)
    shell.run(["rm", "-rf", "--", partial], sudo=True)
    try:
        shell.run(["mkdir", "-p", "--", partial], sudo=True)
        shell.run(["chmod", "700", "--", dest, partial], sudo=True)
        for directory in service.data:
            target = partial / directory.relative_to("/")
            shell.run(["mkdir", "-p", "--", target.parent], sudo=True)
            shell.run(["cp", "-a", "--no-target-directory", "--", directory, target], sudo=True)
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
