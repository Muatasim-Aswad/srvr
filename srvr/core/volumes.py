"""Validate local Docker storage without trusting registered host mountpoint paths."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SrvrError
from .registry import Service, load_registry
from .shell import Shell


@dataclass(frozen=True)
class Volume:
    name: str
    mountpoint: Path
    identity: dict[str, Any]
    filesystem: str
    created_at: str = field(default="", compare=False)

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mountpoint": str(self.mountpoint),
            "filesystem": self.filesystem,
            "created_at": self.created_at,
            **self.identity,
        }


def directory_identity(shell: Shell, path: Path) -> str:
    canonical = shell.run(["readlink", "--canonicalize-existing", "--", path], sudo=True)
    if canonical.stdout.rstrip("\n") != str(path):
        raise SrvrError("data path contains a symlink or changed mapping")
    shell.run(["test", "-d", path], sudo=True)
    return shell.run(["stat", "--format=%d:%i", "--", path], sudo=True).stdout.strip()


def destination(shell: Shell, path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise SrvrError("data destination must be an absolute path without traversal")
    ancestor = path
    while not shell.run(["test", "-e", ancestor], sudo=True, check=False).ok:
        if shell.run(["test", "-L", ancestor], sudo=True, check=False).ok:
            raise SrvrError("data destination contains a broken symlink")
        ancestor = ancestor.parent
    directory_identity(shell, ancestor)


def _local_mounts(shell: Shell, mountpoint: Path) -> None:
    mounted = _json(shell, ["findmnt", "--json", "--list", "--output", "TARGET"])
    if not isinstance(mounted, dict) or not isinstance(mounted.get("filesystems"), list):
        raise SrvrError("cannot validate volume filesystem mounts")
    for item in mounted["filesystems"]:
        if not isinstance(item, dict) or not isinstance(item.get("target"), str):
            raise SrvrError("unreadable volume filesystem mapping")
        target = Path(item.get("target", ""))
        if target == mountpoint or target.is_relative_to(mountpoint):
            raise SrvrError("mounted volume storage and nested mounts are unsupported")


def unchanged(shell: Shell, volume: Volume) -> None:
    if (
        directory_identity(shell, volume.mountpoint) != volume.filesystem
        or directory_identity(shell, volume.mountpoint.parent)
        != volume.identity["volume_filesystem"]
    ):
        raise SrvrError(f"{volume.name}: volume storage changed; keep the service stopped")


def validate_registration(service: Service, registry_directory: Path | None = None) -> None:
    if (
        registry_directory is not None
        and service.source is not None
        and service.source == registry_directory / f"{service.name}.toml"
        and not service.source.is_file()
    ):
        raise SrvrError(f"{service.name}: registration removed; rerun the command")
    if service.source is None or not service.source.exists():
        return
    registry = load_registry(service.source.parent)
    if registry.broken or registry.services.get(service.name) != service:
        raise SrvrError(f"{service.name}: registration changed or ownership is ambiguous")


def _json(shell: Shell, argv: list[str]) -> Any:
    try:
        return json.loads(shell.run(argv, sudo=True).stdout)
    except (json.JSONDecodeError, SrvrError) as exc:
        raise SrvrError("cannot validate Docker volume storage (details hidden)") from exc


def _containers(shell: Shell, service: Service, name: str, *, stopped: bool) -> None:
    ids = shell.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"volume={name}"], sudo=True
    ).stdout.split()
    template = (
        '{"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"running":{{json .State.Running}},"paused":{{json .State.Paused}}}'
    )
    for container in ids:
        state = _json(shell, ["docker", "inspect", "--format", template, container])
        if not isinstance(state, dict) or state.get("project") != service.project:
            raise SrvrError(f"{name}: volume is used outside the registered Compose project")
        if stopped and (state.get("running") is not False or state.get("paused") is not False):
            raise SrvrError(f"{name}: stop every container using this volume before copying data")


def _daemon(shell: Shell) -> tuple[Path, str]:
    host = shell.run(["printenv", "DOCKER_HOST"], sudo=True, check=False).stdout.strip()
    if host and not host.startswith("unix:///"):
        raise SrvrError("named-volume recovery requires a local Docker endpoint")
    endpoint = _json(
        shell, ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"]
    )
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise SrvrError("named-volume recovery requires a local Unix-socket Docker daemon")
    shell.run(["test", "-S", (host or endpoint).removeprefix("unix://")], sudo=True)
    info = _json(
        shell,
        [
            "docker",
            "info",
            "--format",
            '{"ID":{{json .ID}},"DockerRootDir":{{json .DockerRootDir}}}',
        ],
    )
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("ID"), str)
        or not info["ID"]
        or not isinstance(info.get("DockerRootDir"), str)
    ):
        raise SrvrError("cannot identify the local Docker daemon")
    root = Path(info.get("DockerRootDir", ""))
    if not root.is_absolute() or len(root.parts) < 3:
        raise SrvrError("unsafe Docker data root")
    directory_identity(shell, root)
    kind = shell.run(["stat", "--file-system", "--format=%T", "--", root], sudo=True)
    if kind.stdout.strip() not in {"ext2/ext3", "xfs", "btrfs", "tmpfs", "overlayfs", "zfs"}:
        raise SrvrError("named-volume storage requires a supported local filesystem")
    return root, str(info["ID"])


def inspect(shell: Shell, service: Service, *, stopped: bool = True) -> tuple[Volume, ...]:
    validate_registration(service)
    if not service.data_volumes:
        return ()
    root, daemon = _daemon(shell)
    found = []
    for name in service.data_volumes:
        raw = _json(shell, ["docker", "volume", "inspect", "--", name])
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise SrvrError(f"{name}: unreadable volume identity")
        data = raw[0]
        if (
            data.get("Name") != name
            or data.get("Driver") != "local"
            or data.get("Scope") != "local"
            or data.get("Options") not in (None, {})
            or not isinstance(data.get("CreatedAt"), str)
            or not data["CreatedAt"]
        ):
            raise SrvrError(f"{name}: only plain local named volumes are supported")
        mountpoint = root / "volumes" / name / "_data"
        if data.get("Mountpoint") != str(mountpoint):
            raise SrvrError(f"{name}: unsafe Docker volume mountpoint")
        fingerprint = directory_identity(shell, mountpoint)
        _local_mounts(shell, mountpoint)
        if shell.run(["mountpoint", "--quiet", "--", mountpoint], sudo=True, check=False).ok:
            raise SrvrError(f"{name}: mounted volume roots are unsupported")
        labels = data.get("Labels") or {}
        if not isinstance(labels, dict):
            raise SrvrError(f"{name}: unreadable volume ownership")
        project = labels.get("com.docker.compose.project")
        if project is not None and not isinstance(project, str):
            raise SrvrError(f"{name}: unreadable volume project identity")
        if project and project != service.project:
            raise SrvrError(f"{name}: volume belongs to another Compose project")
        _containers(shell, service, name, stopped=stopped)
        if any(
            d == mountpoint or d.is_relative_to(root) or root.is_relative_to(d)
            for d in service.data
        ):
            raise SrvrError("registered directories overlap Docker-managed storage")
        found.append(
            Volume(
                name,
                mountpoint,
                {
                    "driver": "local",
                    "scope": "local",
                    "options": {},
                    "volume_filesystem": directory_identity(shell, mountpoint.parent),
                    "daemon": daemon,
                    "project": project,
                },
                fingerprint,
                data["CreatedAt"],
            )
        )
    return tuple(found)


def revalidate(shell: Shell, service: Service, expected: tuple[Volume, ...]) -> None:
    if inspect(shell, service) != expected:
        raise SrvrError(f"{service.name}: Docker volume identity changed; keep the service stopped")


def journal_matches(record: object, volume: Volume) -> bool:
    if not isinstance(record, dict):
        return False
    expected = volume.record()
    return all(record.get(key) == value for key, value in expected.items() if key != "created_at")


def archive_members(records: object, current: tuple[Volume, ...]) -> dict[str, Path]:
    if not isinstance(records, list) or len(records) != len(current):
        raise SrvrError("backup volume coverage differs from the registry")
    members = {}
    for record, volume in zip(records, current, strict=True):
        if not isinstance(record, dict) or record.get("name") != volume.name:
            raise SrvrError("backup contains an unknown volume mapping")
        expected = volume.record()
        for key in ("driver", "scope", "options", "project"):
            if record.get(key) != expected[key]:
                raise SrvrError(f"{volume.name}: backup volume identity differs")
        if not record.get("daemon") or not record.get("created_at"):
            raise SrvrError(f"{volume.name}: backup volume identity is incomplete")
        if (
            record["daemon"] == expected["daemon"]
            and "volume_filesystem" in record
            and record["volume_filesystem"] != expected["volume_filesystem"]
        ):
            raise SrvrError(f"{volume.name}: volume was replaced since this backup")
        if (
            record["daemon"] == expected["daemon"]
            and record.get("mountpoint") == str(volume.mountpoint)
            and record.get("filesystem") != volume.filesystem
        ):
            raise SrvrError(f"{volume.name}: volume storage was replaced since this backup")
        if not isinstance(record.get("mountpoint"), str):
            raise SrvrError(f"{volume.name}: invalid archived volume path")
        member = Path(record["mountpoint"])
        if (
            not member.is_absolute()
            or ".." in member.parts
            or member.parts[-3:] != ("volumes", volume.name, "_data")
        ):
            raise SrvrError(f"{volume.name}: invalid archived volume path")
        members[volume.name] = member
    return members


def coverage(service: Service) -> str:
    return ", ".join(
        [*(str(d) for d in service.data), *(f"volume:{v}" for v in service.data_volumes)]
    )
