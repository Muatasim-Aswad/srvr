"""Private configuration baselines and guarded, recoverable file replacement."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import atomic, configuration
from .errors import SrvrError
from .paths import Paths, ensure_private_dir
from .registry import Service


@dataclass(frozen=True)
class FileState:
    content: str | None
    mode: int = 0o600
    uid: int = 0
    gid: int = 0
    mtime_ns: int = 0
    identity: tuple[int, int] | None = None


def read(file: Path) -> FileState:
    if file.is_symlink():
        raise SrvrError("configuration symlinks are unsupported (path hidden)")
    if not file.exists():
        return FileState(None)
    before = file.stat()
    content = base64.b64encode(file.read_bytes()).decode("ascii")
    after = file.stat()
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, key) != getattr(after, key) for key in fields) or not file.is_file():
        raise SrvrError("configuration changed while reading (path hidden)")
    return FileState(
        content,
        before.st_mode & 0o7777,
        before.st_uid,
        before.st_gid,
        before.st_mtime_ns,
        (before.st_dev, before.st_ino),
    )


def capture(service: Service, *, extra: tuple[Path, ...] = ()) -> dict[str, FileState]:
    return {str(p): read(p) for p in sorted({*configuration.files(service), *extra})}


def encode(states: dict[str, FileState]) -> dict[str, dict[str, object]]:
    return {path: asdict(state) for path, state in states.items()}


def decode(raw: dict[str, dict[str, object]]) -> dict[str, FileState]:
    result = {}
    for path, state in raw.items():
        item = FileState(**state)  # type: ignore[arg-type]
        if item.identity is not None:
            item = FileState(
                item.content,
                item.mode,
                item.uid,
                item.gid,
                item.mtime_ns,
                (item.identity[0], item.identity[1]),
            )
        result[path] = item
    return result


def validate(expected: dict[str, FileState]) -> None:
    if any(read(Path(path)) != state for path, state in expected.items()):
        raise SrvrError("configuration paths, metadata or contents changed; rerun the command")


def _same_content(left: FileState, right: FileState) -> bool:
    return (left.content, left.mode, left.uid, left.gid, left.mtime_ns) == (
        right.content,
        right.mode,
        right.uid,
        right.gid,
        right.mtime_ns,
    )


def _write(file: Path, state: FileState, expected: FileState) -> None:
    if state.content is None:
        validate({str(file): expected})
        atomic.remove(file)
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{file.name}-srvr-", dir=file.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(base64.b64decode(state.content, validate=True))
            handle.flush()
            os.fchown(handle.fileno(), state.uid, state.gid)
            os.fchmod(handle.fileno(), state.mode)
            os.utime(handle.fileno(), ns=(state.mtime_ns, state.mtime_ns))
            os.fsync(handle.fileno())
        validate({str(file): expected})
        temporary.replace(file)
        atomic.sync_directory(file.parent)
    finally:
        temporary.unlink(missing_ok=True)


def restore(
    paths: Paths,
    service: Service,
    target: dict[str, FileState],
    expected: dict[str, FileState],
) -> None:
    if set(target) != set(expected):
        raise SrvrError("configuration snapshot paths changed; recovery needs administrator review")
    validate(expected)
    journal = paths.service_state(service.name) / "configuration-recovery.json"
    if journal.exists() or journal.is_symlink():
        raise SrvrError("unfinished configuration recovery; recover it before another replacement")
    atomic.write_json(journal, {"target": encode(target), "expected": encode(expected)})
    recover(paths, service)


def recover(paths: Paths, service: Service) -> None:
    journal = paths.service_state(service.name) / "configuration-recovery.json"
    if journal.is_symlink():
        raise SrvrError("configuration recovery journal is a symlink; review required")
    raw = json.loads(journal.read_text())
    target, expected = decode(raw["target"]), decode(raw["expected"])
    allowed = {str(p) for p in configuration.files(service)}
    allowed.add(str(version_file(paths, service)))
    if set(target) != set(expected) or set(target) - allowed:
        raise SrvrError("configuration recovery paths differ from registration; review required")
    for path, state in target.items():
        current = read(Path(path))
        if current != expected[path] and not _same_content(current, state):
            raise SrvrError("concurrent configuration changes prevent recovery (values hidden)")
    for path, state in target.items():
        file = Path(path)
        current = read(file)
        if current != expected[path] and not _same_content(current, state):
            raise SrvrError("concurrent configuration changes prevent recovery (values hidden)")
        if not _same_content(current, state):
            _write(file, state, current)
    atomic.remove(journal)


def baseline_file(paths: Paths, service: Service) -> Path:
    return paths.service_state(service.name) / "configuration-baseline.json"


def version_file(paths: Paths, service: Service) -> Path:
    return paths.service_state(service.name) / "versions.compose.json"


def save_baseline(paths: Paths, service: Service, states: dict[str, FileState]) -> None:
    ensure_private_dir(paths.service_state(service.name))
    atomic.write_json(
        baseline_file(paths, service),
        {"files": encode(states), "registration": registration(service)},
    )


def load_baseline(paths: Paths, service: Service) -> dict[str, FileState]:
    file = baseline_file(paths, service)
    if not file.exists():
        raise SrvrError("no known-good configuration baseline; use update --capture-config first")
    raw = json.loads(file.read_text())
    if raw.get("registration") != registration(service):
        raise SrvrError("registration differs from the known-good baseline; review required")
    return decode(raw["files"])


def registration(service: Service) -> str:
    digest = hashlib.sha256(repr(service).encode())
    if service.source is not None and service.source.exists():
        digest.update(service.source.read_bytes())
    for path in (service.workdir, *service.data):
        stat = path.stat()
        digest.update(repr((str(path.resolve()), stat.st_dev, stat.st_ino)).encode())
    return digest.hexdigest()


def content(state: FileState, value: bytes) -> FileState:
    return FileState(
        base64.b64encode(value).decode("ascii"),
        state.mode if state.content is not None else 0o600,
        state.uid if state.content is not None else os.getuid(),
        state.gid if state.content is not None else os.getgid(),
        state.mtime_ns,
        state.identity,
    )
