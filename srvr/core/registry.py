"""Service registry: one TOML file per service in `~/.config/srvr/services/<name>.toml`."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from .errors import SrvrError

KINDS = ("git", "image")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
PROFILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
VOLUME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")

# key -> (type, required)
SCHEMA: dict[str, tuple[type, bool]] = {
    "kind": (str, True),
    "workdir": (str, True),
    "description": (str, False),
    "project": (str, False),
    "compose_files": (list, False),
    "env_file": (str, False),
    "config_files": (list, False),
    "branch": (str, False),
    "migrate": (str, False),
    "data": (list, False),
    "data_volumes": (list, False),
    "profiles": (list, False),
    "allow_public_ports": (bool, False),
    "allow_host_network": (bool, False),
    "health": (str, False),
    "url": (str, False),
    "version_url": (str, False),
    "update_component": (str, False),
}


@dataclass(frozen=True)
class Service:
    name: str
    kind: str  # "git": built from a Git checkout; "image": pinned published image
    workdir: Path  # Compose project directory (the checkout for git services)
    project: str  # Compose project name
    compose_files: tuple[Path, ...]
    env_file: Path | None  # passed to Compose as --env-file (interpolation)
    config_files: tuple[Path, ...]  # settings shown and edited by `srvr config`
    branch: str | None  # default deploy branch (git services)
    migrate: str | None  # one-off Compose service run before starting (e.g. "migrate")
    data: tuple[Path, ...]  # persistent directories included in backups
    health: str | None  # loopback health URL
    url: str | None  # private HTTPS URL
    version_url: str | None = None  # loopback build record (version.json), git services
    description: str = ""
    source: Path | None = None  # registry file
    update_component: str | None = None  # default component for image updates
    data_volumes: tuple[str, ...] = ()  # actual Docker volume names, never mountpoint paths
    profiles: tuple[str, ...] = ()
    allow_public_ports: bool = False
    allow_host_network: bool = False

    @property
    def is_git(self) -> bool:
        return self.kind == "git"

    @property
    def has_data(self) -> bool:
        return bool(self.data or self.data_volumes)


def _check_type(path: Path, key: str, value: Any, expected: type) -> None:
    if not isinstance(value, expected):
        raise SrvrError(f"{path}: {key} must be a {expected.__name__}")
    if isinstance(value, list) and not all(isinstance(item, str) for item in value):
        raise SrvrError(f"{path}: {key} must be a list of strings")


def _absolute(path: Path, key: str, value: str, base: Path | None = None) -> Path:
    resolved = Path(value).expanduser()
    if not resolved.is_absolute():
        if base is None:
            raise SrvrError(f"{path}: {key} must be an absolute path: {value}")
        resolved = base / resolved
    return resolved


def _check_kind(path: Path, raw: dict[str, Any]) -> str:
    kind: str = raw["kind"]
    if kind not in KINDS:
        raise SrvrError(f"{path}: kind must be one of {', '.join(KINDS)}")
    if kind == "git" and not raw.get("branch"):
        raise SrvrError(f"{path}: git services need a branch")
    if kind == "image":
        for key in ("branch", "version_url"):
            if key in raw:
                raise SrvrError(f"{path}: image services cannot have {key}")
    elif "update_component" in raw:
        raise SrvrError(f"{path}: update_component is only for image services")
    return kind


def _check_data(path: Path, data: tuple[Path, ...]) -> None:
    for directory in data:
        if len(directory.parts) < 3 or directory == Path.home():
            raise SrvrError(f"{path}: data path is too broad to back up: {directory}")
    for index, directory in enumerate(data):
        for other in data[index + 1 :]:
            if directory.is_relative_to(other) or other.is_relative_to(directory):
                raise SrvrError(f"{path}: data directories overlap: {directory}, {other}")


def parse_service(name: str, raw: dict[str, Any], path: Path) -> Service:
    """Validate one registry entry; errors name the file and key."""
    if not NAME_RE.match(name):
        raise SrvrError(f"{path}: service name must match {NAME_RE.pattern}")
    if unknown := sorted(set(raw) - set(SCHEMA)):
        raise SrvrError(f"{path}: unknown key(s): {', '.join(unknown)}")
    for key, (expected, required) in SCHEMA.items():
        if key in raw:
            _check_type(path, key, raw[key], expected)
        elif required:
            raise SrvrError(f"{path}: missing required key: {key}")

    kind = _check_kind(path, raw)

    if "compose_files" in raw and not raw["compose_files"]:
        raise SrvrError(f"{path}: compose_files must not be empty")

    workdir = _absolute(path, "workdir", raw["workdir"])
    env_file = _absolute(path, "env_file", raw["env_file"]) if "env_file" in raw else None
    data = tuple(_absolute(path, "data", item) for item in raw.get("data", []))
    _check_data(path, data)
    for key, pattern in (("profiles", PROFILE_RE), ("data_volumes", VOLUME_RE)):
        values = raw.get(key, [])
        if len(set(values)) != len(values) or any(not pattern.fullmatch(v) for v in values):
            raise SrvrError(f"{path}: {key} must contain unique valid names")
    for key in ("health", "url", "version_url"):
        if key in raw and not raw[key].startswith(("http://", "https://")):
            raise SrvrError(f"{path}: {key} must be an http(s) URL")

    return Service(
        name=name,
        kind=kind,
        workdir=workdir,
        project=raw.get("project", name),
        compose_files=tuple(
            _absolute(path, "compose_files", item, workdir)
            for item in raw.get("compose_files", ["compose.yaml"])
        ),
        env_file=env_file,
        config_files=tuple(_absolute(path, "config_files", item) for item in raw["config_files"])
        if "config_files" in raw
        else ((env_file,) if env_file else ()),
        branch=raw.get("branch"),
        migrate=raw.get("migrate"),
        data=data,
        health=raw.get("health"),
        url=raw.get("url"),
        version_url=raw.get("version_url"),
        description=raw.get("description", ""),
        source=path,
        update_component=raw.get("update_component"),
        data_volumes=tuple(raw.get("data_volumes", [])),
        profiles=tuple(raw.get("profiles", [])),
        allow_public_ports=raw.get("allow_public_ports", False),
        allow_host_network=raw.get("allow_host_network", False),
    )


def load_service(path: Path) -> Service:
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SrvrError(f"{path}: {exc}") from exc
    return parse_service(path.stem, raw, path)


@dataclass(frozen=True)
class Registry:
    services: dict[str, Service]  # valid entries, sorted by name
    broken: dict[str, str]  # service name -> why its file could not be loaded


def conflict(first: Service, second: Service) -> str | None:
    if first.project == second.project or first.workdir.resolve() == second.workdir.resolve():
        return "project or workdir"
    for left in first.data:
        for right in second.data:
            a, b = left.resolve(), right.resolve()
            if a.is_relative_to(b) or b.is_relative_to(a):
                return "data directories"
    if set(first.data_volumes) & set(second.data_volumes):
        return "named volumes"
    return None


def load_registry(directory: Path) -> Registry:
    """Load every `*.toml` service file. A broken file affects only its own service."""
    if not directory.is_dir():
        raise SrvrError(f"no service registry at {directory}; add <name>.toml files there")
    files = sorted(directory.glob("*.toml"))
    if not files:
        raise SrvrError(f"no services registered in {directory}")
    services: dict[str, Service] = {}
    broken: dict[str, str] = {}
    for path in files:
        try:
            service = load_service(path)
        except (SrvrError, OSError) as exc:
            broken[path.stem] = str(exc)
        else:
            services[service.name] = service
    for first, second in combinations(services.values(), 2):
        if shared := conflict(first, second):
            broken[first.name] = f"{shared} conflicts with {second.name}"
            broken[second.name] = f"{shared} conflicts with {first.name}"
    for name in broken:
        services.pop(name, None)
    return Registry(services, broken)


def select(registry: Registry, names: Iterable[str]) -> list[Service]:
    """Resolve names to services in the order given; an empty selection means all valid ones."""
    names = list(dict.fromkeys(names))
    if not names:
        return list(registry.services.values())
    for name in names:
        if name in registry.broken:
            raise SrvrError(f"{name}: broken service file: {registry.broken[name]}")
    if unknown := [name for name in names if name not in registry.services]:
        known = ", ".join(registry.services)
        raise SrvrError(f"unknown service(s): {', '.join(unknown)} (known: {known})")
    return [registry.services[name] for name in names]
