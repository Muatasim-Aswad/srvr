"""Validate existing projects and save their service registration without changing them."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .atomic import sync_directory
from .composefile import validate_image_ref
from .errors import SrvrError
from .paths import Paths, ensure_private_dir, keep_home_owner
from .registry import Service, conflict, load_registry, parse_service


def validate_files(service: Service) -> None:
    if not service.workdir.is_dir():
        raise SrvrError(f"project directory does not exist: {service.workdir}")
    for file in (*service.compose_files, *service.config_files):
        if not file.is_file():
            raise SrvrError(f"file does not exist: {file}")
    if service.env_file is not None and not service.env_file.is_file():
        raise SrvrError(f"env file does not exist: {service.env_file}")
    for directory in service.data:
        if not directory.is_dir():
            raise SrvrError(f"prepare the data directory and its container ownership: {directory}")


def writable_binds(model: dict[str, Any]) -> list[Path]:
    return sorted(
        {
            Path(volume["source"])
            for spec in model.get("services", {}).values()
            for volume in spec.get("volumes", [])
            if volume.get("type") == "bind" and not volume.get("read_only", False)
        }
    )


def validate_components(service: Service, specs: dict[str, Any]) -> list[str]:
    warnings = []
    for component, spec in specs.items():
        if spec.get("network_mode") == "host":
            raise SrvrError(f"{component}: host networking cannot ensure loopback-only publication")
        for port in spec.get("ports", []):
            if port.get("host_ip") not in ("127.0.0.1", "::1"):
                raise SrvrError(f"{component}: published ports must bind to 127.0.0.1 or ::1")
        if not service.is_git and "image" in spec and "build" not in spec:
            try:
                validate_image_ref(spec["image"])
            except SrvrError as exc:
                raise SrvrError(f"{component}: {exc}") from exc
        if any(volume.get("type") == "volume" for volume in spec.get("volumes", [])):
            warnings.append(f"{component}: named/anonymous volumes are not backed up by srvr")
    return warnings


def validate_model(service: Service, model: dict[str, Any]) -> list[str]:
    specs = model.get("services", {})
    if not specs:
        raise SrvrError("Compose project has no services")
    if service.migrate and service.migrate not in specs:
        raise SrvrError(f"migration component does not exist: {service.migrate}")
    if service.update_component:
        spec = specs.get(service.update_component, {})
        if "image" not in spec or "build" in spec:
            raise SrvrError(f"not an image component: {service.update_component}")
    warnings = validate_components(service, specs)
    for source in writable_binds(model):
        if not any(
            source == directory or source.is_relative_to(directory) for directory in service.data
        ):
            warnings.append(f"writable bind mount is outside registered backup data: {source}")
    return warnings


def save_registration(paths: Paths, service: Service, raw: dict[str, Any]) -> Path:
    target = paths.services_dir / f"{service.name}.toml"
    text = (
        "\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in raw.items())
        + "\n"
    )
    parse_service(service.name, raw, target)
    ensure_private_dir(paths.services_dir)
    descriptor, name = tempfile.mkstemp(prefix=".add-", dir=paths.services_dir)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        keep_home_owner(temporary)
        try:
            os.link(temporary, target)
            sync_directory(target.parent)
        except FileExistsError as exc:
            raise SrvrError(f"{service.name}: already registered; use srvr config/info") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def validate_unique(paths: Paths, service: Service) -> None:
    if not any(paths.services_dir.glob("*.toml")):
        return
    for other in load_registry(paths.services_dir).services.values():
        if shared := conflict(service, other):
            raise SrvrError(f"{shared} is already registered as {other.name}")
