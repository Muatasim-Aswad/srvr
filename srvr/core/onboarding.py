"""Validate existing projects and save their service registration without changing them."""

from __future__ import annotations

import hashlib
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
            if not service.allow_host_network:
                raise SrvrError(f"{component}: host networking needs allow_host_network = true")
            warnings.append(f"{component}: host networking bypasses published-port restrictions")
        for port in spec.get("ports", []):
            if port.get("host_ip") not in ("127.0.0.1", "::1"):
                if not service.allow_public_ports:
                    raise SrvrError(f"{component}: published ports must bind to 127.0.0.1 or ::1")
                warnings.append(f"{component}: non-loopback publication is explicitly allowed")
        if not service.is_git and "image" in spec:
            try:
                validate_image_ref(spec["image"])
            except SrvrError as exc:
                raise SrvrError(
                    f"{component}: image must have a valid explicit version (value hidden)"
                ) from exc
    return warnings


def registration_state(service: Service) -> dict[Path, tuple[int, int, int, str]]:
    """Private fingerprints for detecting changes during confirmation."""
    files = set((*service.compose_files, *service.config_files))
    if service.env_file:
        files.add(service.env_file)
    elif (service.workdir / ".env").exists():
        files.add(service.workdir / ".env")
    state = {}
    for file in files:
        stat = file.stat()
        state[file] = (
            stat.st_dev,
            stat.st_ino,
            stat.st_mode,
            hashlib.sha256(file.read_bytes()).hexdigest(),
        )
    for directory in (service.workdir, *service.data):
        stat = directory.stat()
        state[directory] = (stat.st_dev, stat.st_ino, stat.st_mode, "")
    return state


def selected_services(service: Service, model: dict[str, Any]) -> dict[str, Any]:
    """Operational components; unprofiled services always participate."""
    return {
        name: spec
        for name, spec in model.get("services", {}).items()
        if not spec.get("profiles") or set(spec["profiles"]) & set(service.profiles)
    }


def named_volumes(model: dict[str, Any]) -> dict[str, str]:
    """Mounted Compose keys mapped to resolved Docker names, including external volumes."""
    definitions = model.get("volumes", {})
    return {
        key: definitions.get(key, {}).get("name", "")
        for spec in model.get("services", {}).values()
        for mount in spec.get("volumes", [])
        if mount.get("type") == "volume" and (key := mount.get("source"))
    }


def validate_profiles(service: Service, model: dict[str, Any]) -> None:
    declared = {
        profile
        for spec in model.get("services", {}).values()
        for profile in spec.get("profiles", [])
    }
    if set(service.profiles) - declared:
        raise SrvrError("registered profiles are not declared in the Compose project")


def validate_model(service: Service, model: dict[str, Any]) -> list[str]:
    """Validate the all-profile model; backup coverage applies to selected components."""
    specs = model.get("services", {})
    if not specs:
        raise SrvrError("Compose project has no services")
    validate_profiles(service, model)
    selected = selected_services(service, model)
    if not selected:
        raise SrvrError("registered profiles select no Compose services")
    if service.migrate and service.migrate not in specs:
        raise SrvrError(f"migration component does not exist: {service.migrate}")
    from .startup import validate_dependencies

    validate_dependencies(service, model)
    if service.update_component:
        spec = selected.get(service.update_component, {})
        if "image" not in spec:
            raise SrvrError(f"not an image component: {service.update_component}")
    required = dict(selected)
    if service.migrate:
        required[service.migrate] = specs[service.migrate]
    for component, spec in required.items():
        if not service.is_git and not spec.get("image"):
            raise SrvrError(f"{component}: image mode requires an explicit published image")
    warnings = validate_components(service, specs)
    active_model = {**model, "services": selected}
    return warnings + _backup_warnings(service, active_model)


def _backup_warnings(service: Service, active_model: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    selected = active_model["services"]
    mounted = named_volumes(active_model)
    if set(service.data_volumes) - set(mounted.values()):
        raise SrvrError(
            "registered data_volumes must be resolved named volumes in selected services"
        )
    for component, spec in selected.items():
        for mount in spec.get("volumes", []):
            if mount.get("type") != "volume":
                continue
            key = mount.get("source")
            if not key:
                warnings.append(f"{component}: anonymous volume is unmanaged and not backed up")
            elif mounted.get(key) not in service.data_volumes:
                warnings.append(
                    f"{component}: named volume is outside registered backup data: {key}"
                )
    for source in writable_binds(active_model):
        if not any(
            source == directory or source.is_relative_to(directory) for directory in service.data
        ):
            warnings.append("writable bind mount is outside registered backup data (path hidden)")
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
    registry = load_registry(paths.services_dir)
    if registry.broken:
        raise SrvrError("repair broken registrations before checking exclusive data ownership")
    for other in registry.services.values():
        if shared := conflict(service, other):
            raise SrvrError(f"{shared} is already registered as {other.name}")
