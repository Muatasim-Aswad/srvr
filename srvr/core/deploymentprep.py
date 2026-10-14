"""Resolve staged Compose candidates and retain prepared images before switching."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import atomic, configsnapshots, envfile
from .compose import Compose
from .composefile import find_image, validate_image_ref
from .context import Context
from .errors import SrvrError
from .onboarding import selected_services, validate_model
from .registry import Service


def configured(ctx: Context, service: Service) -> Service:
    version = configsnapshots.version_file(ctx.paths, service)
    return (
        replace(service, compose_files=(*service.compose_files, version))
        if version.exists() and version not in service.compose_files
        else service
    )


def compose(ctx: Context, service: Service) -> Compose:
    return ctx.compose(configured(ctx, service))


def check_model(service: Service, model: dict[str, Any]) -> None:
    validate_model(service, model)
    for spec in model.get("services", {}).values():
        if "build" not in spec:
            try:
                validate_image_ref(spec.get("image", ""))
            except SrvrError as exc:
                raise SrvrError(
                    "every prebuilt image needs an explicit version (value hidden)"
                ) from exc


class Candidate:
    def __init__(
        self,
        ctx: Context,
        service: Service,
        files: Mapping[Path, bytes] | None = None,
        *,
        images: Mapping[str, str] | None = None,
        commit: str | None = None,
    ) -> None:
        self.ctx, self.service = ctx, service
        self._temporary = tempfile.TemporaryDirectory(prefix="srvr-candidate-")
        self.root = Path(self._temporary.name)
        self.compose: Compose
        try:
            self._stage(files or {}, images or {}, commit)
        except BaseException:
            self.close()
            raise

    def _stage(
        self, files: Mapping[Path, bytes], images: Mapping[str, str], commit: str | None
    ) -> None:
        service = configured(self.ctx, self.service)
        work = self.root / "project"
        tracked: set[str] = set()
        if commit is not None:
            self.ctx.shell.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--shared",
                    "--no-checkout",
                    str(service.workdir),
                    str(work),
                ]
            )
            self.ctx.shell.run(["git", "-C", str(work), "checkout", "--quiet", "--detach", commit])
            tracked = set(
                self.ctx.shell.run(
                    ["git", "-C", str(service.workdir), "ls-files", "-z"]
                ).stdout.split("\0")
            )
        else:

            def ignore(directory: str, names: list[str]) -> list[str]:
                return [
                    name
                    for name in names
                    if name == ".git"
                    or any((Path(directory) / name).is_relative_to(data) for data in service.data)
                ]

            shutil.copytree(service.workdir, work, symlinks=True, ignore=ignore)
        mapping: dict[Path, Path] = {}
        registered = {*service.compose_files, *service.config_files, service.workdir / ".env"}
        registered.update(p for p in (service.env_file,) if p is not None)
        for index, path in enumerate(sorted(registered)):
            if path.is_relative_to(service.workdir):
                target = work / path.relative_to(service.workdir)
            else:
                target = self.root / "external" / str(index) / path.name
            mapping[path] = target
            if path in files or (
                not target.exists()
                and path.exists()
                and (
                    commit is None
                    or not path.is_relative_to(service.workdir)
                    or str(path.relative_to(service.workdir)) not in tracked
                )
            ):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(files[path] if path in files else path.read_bytes())
                target.chmod(0o600)
        self._mapping = mapping
        for original in service.compose_files:
            file = mapping[original]
            if not file.exists():
                raise SrvrError("target lacks a registered Compose file (path hidden)")
            text = file.read_text()
            for source, target in mapping.items():
                text = text.replace(str(source), str(target))
                relative = os.path.relpath(source, service.compose_files[0].parent)
                if relative.startswith("../"):
                    text = text.replace(relative, str(target))
            file.write_text(text)
            file.chmod(0o600)
        service = replace(
            service,
            workdir=work,
            compose_files=tuple(mapping[p] for p in service.compose_files),
            env_file=mapping[service.env_file] if service.env_file else None,
            config_files=tuple(mapping[p] for p in service.config_files),
        )
        required = (*service.compose_files, *service.config_files)
        required += (service.env_file,) if service.env_file else ()
        if any(not file.is_file() for file in required):
            raise SrvrError("target lacks a registered configuration file (path hidden)")
        if images:
            override = self.root / "versions.compose.json"
            atomic.write_json(
                override, {"services": {name: {"image": ref} for name, ref in images.items()}}
            )
            service = replace(service, compose_files=(*service.compose_files, override))
        self.compose = Compose(service, self.ctx.shell)

    def model(self) -> dict[str, Any]:
        model = self.compose.config(all_profiles=True)
        check_model(self.service, model)
        return model

    def comparable_model(self) -> dict[str, Any]:
        """Map temporary build/project paths back to the registered project."""

        def original(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: original(item) for key, item in value.items()}
            if isinstance(value, list):
                return [original(item) for item in value]
            if isinstance(value, str):
                for source, target in self._mapping.items():
                    value = value.replace(str(target), str(source))
                value = value.replace(str(self.compose.service.workdir), str(self.service.workdir))
            return value

        return original(self.model())  # type: ignore[no-any-return]

    def prepare(
        self,
        *,
        stamp: Mapping[str, str] | None = None,
        components: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        model = self.model()
        self.prepared_model = model
        specs = selected_services(self.service, model)
        if components is not None:
            specs = {name: spec for name, spec in specs.items() if name in components}
        if self.service.migrate:
            specs[self.service.migrate] = model["services"][self.service.migrate]
        built = [name for name, spec in specs.items() if self.service.is_git and "build" in spec]
        pulled = [name for name in specs if name not in built]
        for action, targets in (("build", built), ("pull", pulled)):
            if not targets:
                continue
            prefix = (
                ["env", *(f"{k}={v}" for k, v in (stamp or {}).items())]
                if action == "build" and stamp
                else []
            )
            resolved = self.compose.config(all_profiles=True, stamp=stamp) if prefix else model
            with self.compose.resolved(
                resolved,
                build=action == "build",
                pull_policy="build" if action == "build" else "always",
            ) as compose:
                argv = compose.argv(action, *targets, runtime=False, all_profiles=True)
                result = self.ctx.shell.run(
                    [*prefix, *argv], sudo=True, check=False, cwd=self.compose.service.workdir
                )
            if not result.ok:
                raise SrvrError(
                    f"candidate {action} failed (details hidden); current installation kept"
                )
        return retain_images(self.ctx, self.service, specs)

    def close(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> Candidate:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def retain_images(ctx: Context, service: Service, specs: dict[str, Any]) -> dict[str, str]:
    images = {}
    for name, spec in specs.items():
        ref = spec.get("image", f"{service.project}-{name}")
        result = ctx.shell.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
            sudo=True,
            check=False,
        )
        image = result.stdout.strip()
        if not result.ok or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise SrvrError("cannot resolve an exact prepared image ID (reference hidden)")
        ctx.docker.tag(image, f"srvr-ready/{service.project}-{name}:latest")
        images[name] = image
    return images


def versions(ctx: Context, service: Service) -> dict[str, str]:
    file = configsnapshots.version_file(ctx.paths, service)
    if not file.exists():
        return {}
    raw = json.loads(file.read_text())
    return {name: spec["image"] for name, spec in raw["services"].items()}


def display_refs(ctx: Context, service: Service, refs: dict[str, str]) -> dict[str, str]:
    owned = versions(ctx, service)
    known = dict(owned)
    for file in service.compose_files:
        try:
            text = file.read_text()
        except OSError:
            continue
        for name in refs:
            try:
                if text.lstrip().startswith("{"):
                    value = json.loads(text).get("services", {}).get(name, {}).get("image")
                else:
                    _, value = find_image(text, name)
            except (SrvrError, json.JSONDecodeError):
                continue
            if value is not None and name not in owned:
                known[name] = value
    return {
        name: envfile.display_value("IMAGE", ref)
        if known.get(name) == ref
        else "(configured, hidden)"
        for name, ref in refs.items()
    }
