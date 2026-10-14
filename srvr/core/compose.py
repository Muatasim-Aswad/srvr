"""Docker Compose invocation for one registered service."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import atomic, envfile
from .errors import SrvrError
from .onboarding import selected_services, validate_model
from .paths import ensure_private_dir
from .registry import Service
from .shell import Shell


class Compose:
    def __init__(self, service: Service, shell: Shell, *, runtime_file: Path | None = None) -> None:
        self.service = service
        self.shell = shell
        self.runtime_file = runtime_file

    def argv(self, *args: str, runtime: bool = True, all_profiles: bool = False) -> list[str]:
        s = self.service
        self._check_ambient_profiles()
        argv = [
            "env",
            "COMPOSE_PROFILES=",
            "docker",
            "compose",
            "--project-name",
            s.project,
            "--project-directory",
            str(s.workdir),
        ]
        if s.env_file:
            argv += ["--env-file", str(s.env_file)]
        for profile in ("*",) if all_profiles else s.profiles:
            argv += ["--profile", profile]
        for file in s.compose_files:
            argv += ["--file", str(file)]
        if runtime and self.runtime_file is not None and self.runtime_file.exists():
            argv += ["--file", str(self.runtime_file)]
        return argv + list(args)

    def _check_ambient_profiles(self) -> None:
        if os.environ.get("COMPOSE_PROFILES"):
            raise SrvrError("unset COMPOSE_PROFILES; selection comes from registered profiles")
        files = (
            [self.service.env_file] if self.service.env_file else [self.service.workdir / ".env"]
        )
        for file in files:
            if (
                file is not None
                and file.is_file()
                and any(
                    key == "COMPOSE_PROFILES" and value
                    for key, value in envfile.parse(file.read_text())
                )
            ):
                raise SrvrError("remove COMPOSE_PROFILES from env files; register profiles instead")

    def pin_images(self, images: Mapping[str, str]) -> None:
        """Keep captured images in a state-owned override, without editing application files."""
        if self.runtime_file is None:
            raise SrvrError("image pinning needs a runtime state path")
        if not images:
            if self.runtime_file.exists():
                atomic.remove(self.runtime_file)
            return
        ensure_private_dir(self.runtime_file.parent)
        model = {
            "services": {
                name: {"image": image, "pull_policy": "never"} for name, image in images.items()
            }
        }
        atomic.write_json(self.runtime_file, model)

    def _run(self, *args: str) -> None:
        if args[0] in ("up", "start", "restart", "run"):
            model = self.config(
                all_profiles=args[0] == "up"
                or (args[0] == "run" and args[-1] == self.service.migrate)
            )
            if args[0] == "run" and args[-1] not in model.get("services", {}):
                raise SrvrError("run component is outside registered profile selection")
        if args[0] == "up":
            policy = "never" if "--pull" in args else "missing"
            with self.resolved(model, build=False, pull_policy=policy, pins=True) as compose:
                self.shell.run(
                    compose.argv(*args), sudo=True, capture=False, cwd=self.service.workdir
                )
        else:
            self.shell.run(self.argv(*args), sudo=True, capture=False, cwd=self.service.workdir)

    def config(
        self,
        *,
        all_profiles: bool = False,
        timeout: float | None = None,
        stamp: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fully resolved Compose model. Rendering needs no Docker daemon, so no sudo."""
        argv = self.argv("config", "--format", "json", runtime=False, all_profiles=True)
        if stamp:
            argv = ["env", *(f"{key}={value}" for key, value in stamp.items()), *argv]
        result = self.shell.run(argv, check=False, timeout=timeout)
        if not result.ok:
            result = self.shell.run(argv, sudo=True, check=False, timeout=timeout)
        if not result.ok:
            raise SrvrError(f"{self.service.name}: Compose configuration failed (details hidden)")
        try:
            model: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SrvrError(f"{self.service.name}: unreadable compose config") from exc
        validate_model(self.service, model)
        if not all_profiles:
            model = {**model, "services": selected_services(self.service, model)}
        return model

    @contextmanager
    def resolved(
        self,
        model: dict[str, Any],
        *,
        build: bool,
        pull_policy: str,
        pins: bool = False,
    ) -> Iterator[Compose]:
        """Execute a resolved model with explicit preparation/runtime policy in private state."""
        model = deepcopy(model)
        specs = selected_services(self.service, model)
        if self.service.migrate and self.service.migrate in model["services"]:
            specs[self.service.migrate] = model["services"][self.service.migrate]
        model["services"] = specs
        pinned = (
            json.loads(self.runtime_file.read_text()).get("services", {})
            if pins and self.runtime_file is not None and self.runtime_file.exists()
            else {}
        )
        for name, spec in model["services"].items():
            if name in pinned:
                spec["image"] = pinned[name]["image"]
            elif "build" in spec and not spec.get("image") and (build or pins):
                spec["image"] = f"{self.service.project}-{name}"
            source_build = self.service.is_git and "build" in spec
            if not build:
                spec.pop("build", None)
            spec["pull_policy"] = (
                "never" if pins and (name in pinned or source_build) else pull_policy
            )

        with tempfile.TemporaryDirectory(prefix="srvr-compose-") as directory:
            file = Path(directory) / "compose.json"
            atomic.write_json(file, model)
            yield Compose(replace(self.service, compose_files=(file,)), self.shell)

    def image_refs(self) -> dict[str, str]:
        """Compose service -> configured image reference."""
        return {
            name: spec["image"]
            for name, spec in self.config().get("services", {}).items()
            if "image" in spec
        }

    def build(self, stamp: Mapping[str, str] | None = None) -> None:
        """`stamp`: variables the build reads; `env` carries them past sudo's environment reset."""
        self.config()
        assignments = [f"{name}={value}" for name, value in (stamp or {}).items()]
        prefix = ["env", *assignments] if assignments else []
        self.shell.run(
            [*prefix, *self.argv("build", runtime=False)],
            sudo=True,
            capture=False,
            cwd=self.service.workdir,
        )

    def pull(self, *components: str) -> None:
        model = self.config()
        if set(components) - set(model.get("services", {})):
            raise SrvrError("pull component is outside registered profile selection")
        targets = list(components) or list(model["services"])
        if self.service.is_git:
            targets = [name for name in targets if "build" not in model["services"][name]]
        if targets:
            with self.resolved(model, build=False, pull_policy="always") as compose:
                self.shell.run(
                    compose.argv("pull", *targets, runtime=False),
                    sudo=True,
                    capture=False,
                    cwd=self.service.workdir,
                )

    def up(self, *, pull: bool = True) -> None:
        """Create or recreate containers whose image or config changed; never builds."""
        self._run(
            "up",
            "--detach",
            "--no-build",
            "--remove-orphans",
            *([] if pull else ["--pull", "never"]),
        )

    def up_component(self, component: str) -> None:
        self._run("up", "--detach", "--no-deps", "--no-build", "--pull", "never", component)

    def start(self) -> None:
        self._run("start")

    def stop(self) -> None:
        ids = self.shell.run(
            [
                "docker",
                "ps",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.service.project}",
                "--filter",
                "label=com.docker.compose.oneoff=True",
            ],
            sudo=True,
        ).stdout.split()
        if ids:
            self.shell.run(["docker", "stop", *ids], sudo=True)
        self._run("stop")

    def restart(self) -> None:
        self._run("restart")

    def run_once(self, component: str, *, pull: bool = True, timeout: float | None = None) -> None:
        model = self.config(all_profiles=True)
        active = selected_services(self.service, model)
        if component not in active and component != self.service.migrate:
            raise SrvrError("run component is outside registered profile selection")
        name = f"srvr-migrate-{self.service.project}-{uuid.uuid4().hex}"
        args = [
            "run",
            "--rm",
            "--name",
            name,
            "--no-deps",
            *([] if pull else ["--pull", "never"]),
            component,
        ]
        try:
            with self.resolved(model, build=False, pull_policy="never", pins=True) as compose:
                result = self.shell.run(
                    compose.argv(*args),
                    sudo=True,
                    check=False,
                    timeout=timeout,
                    cwd=self.service.workdir,
                )
        except BaseException:
            self.shell.run(["docker", "rm", "--force", name], sudo=True, check=False)
            raise
        if not result.ok:
            raise SrvrError("migration failed (details hidden)")

    def passthrough(self, args: list[str]) -> int:
        """Run arbitrary Compose arguments interactively; returns Compose's exit status."""
        if not args or args[0].startswith("-"):
            raise SrvrError("give a Compose subcommand; global overrides are not supported")
        model = self.config(all_profiles=True)
        excluded = set(model.get("services", {})) - set(selected_services(self.service, model))
        if args[0] == "run" and self.service.migrate:
            excluded.discard(self.service.migrate)
        for arg in args[1:]:
            option = arg.split("=", 1)[0]
            if option in (
                "--profile",
                "--file",
                "--env-file",
                "--project-directory",
                "--project-name",
            ):
                raise SrvrError("Compose configuration and profiles are fixed by the registration")
            if arg.startswith("-f") and args[0] != "logs":
                raise SrvrError("Compose files are fixed by the service registration")
            if arg.startswith("-p") and args[0] != "run":
                raise SrvrError("Compose project is fixed by the service registration")
            if args[0] == "run" and (option == "--publish" or arg.startswith("-p")):
                raise SrvrError("configure published ports in registered Compose files")
            if arg in excluded:
                raise SrvrError("Compose component is outside registered profile selection")
            if args[0] in ("run", "exec") and arg in model.get("services", {}):
                break
        command = self.argv(*args, runtime=args[0] not in ("build", "pull"))
        return self.shell.run(
            command, sudo=True, capture=False, check=False, cwd=self.service.workdir
        ).returncode

    def logs(self, *, tail: int, follow: bool, since: str | None, component: str | None) -> None:
        args = ["logs", "--tail", str(tail)]
        if follow:
            args.append("--follow")
        if since:
            args += ["--since", since]
        if component:
            args.append(component)
        self._run(*args)
