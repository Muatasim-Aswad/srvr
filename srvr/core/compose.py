"""Docker Compose invocation for one registered service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import atomic
from .errors import SrvrError
from .paths import ensure_private_dir
from .registry import Service
from .shell import Shell


class Compose:
    def __init__(self, service: Service, shell: Shell, *, runtime_file: Path | None = None) -> None:
        self.service = service
        self.shell = shell
        self.runtime_file = runtime_file

    def argv(self, *args: str, runtime: bool = True) -> list[str]:
        s = self.service
        argv = [
            "docker",
            "compose",
            "--project-name",
            s.project,
            "--project-directory",
            str(s.workdir),
        ]
        if s.env_file:
            argv += ["--env-file", str(s.env_file)]
        for file in s.compose_files:
            argv += ["--file", str(file)]
        if runtime and self.runtime_file is not None and self.runtime_file.exists():
            argv += ["--file", str(self.runtime_file)]
        return argv + list(args)

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
        self.shell.run(self.argv(*args), sudo=True, capture=False, cwd=self.service.workdir)

    def config(self, *, all_profiles: bool = False) -> dict[str, Any]:
        """Fully resolved Compose model. Rendering needs no Docker daemon, so no sudo."""
        profiles = ["--profile", "*"] if all_profiles else []
        argv = self.argv(*profiles, "config", "--format", "json", runtime=False)
        result = self.shell.run(argv, check=False)
        if not result.ok:
            result = self.shell.run(argv, sudo=True)
        try:
            model: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SrvrError(f"{self.service.name}: unreadable compose config") from exc
        return model

    def image_refs(self) -> dict[str, str]:
        """Compose service -> configured image reference."""
        return {
            name: spec["image"]
            for name, spec in self.config().get("services", {}).items()
            if "image" in spec
        }

    def build(self, stamp: Mapping[str, str] | None = None) -> None:
        """`stamp`: variables the build reads; `env` carries them past sudo's environment reset."""
        assignments = [f"{name}={value}" for name, value in (stamp or {}).items()]
        prefix = ["env", *assignments] if assignments else []
        self.shell.run(
            [*prefix, *self.argv("build", runtime=False)],
            sudo=True,
            capture=False,
            cwd=self.service.workdir,
        )

    def pull(self, *components: str) -> None:
        self.shell.run(
            self.argv("pull", "--ignore-buildable", *components, runtime=False),
            sudo=True,
            capture=False,
            cwd=self.service.workdir,
        )

    def up(self, *, pull: bool = True) -> None:
        """Create or recreate containers whose image or config changed; never builds."""
        self._run("up", "--detach", "--no-build", *([] if pull else ["--pull", "never"]))

    def start(self) -> None:
        self._run("start")

    def stop(self) -> None:
        self._run("stop")

    def restart(self) -> None:
        self._run("restart")

    def run_once(self, component: str, *, pull: bool = True) -> None:
        self._run("run", "--rm", "--no-deps", *([] if pull else ["--pull", "never"]), component)

    def passthrough(self, args: list[str]) -> int:
        """Run arbitrary Compose arguments interactively; returns Compose's exit status."""
        command = self.argv(*args, runtime=not args or args[0] not in ("build", "pull"))
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
