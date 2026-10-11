"""Docker Engine queries: container state across Compose projects, image IDs, tags."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .shell import Shell

_FIELDS = (
    '{{.Label "com.docker.compose.project"}}',
    '{{.Label "com.docker.compose.service"}}',
    "{{.Names}}",
    "{{.State}}",
    "{{.Status}}",
    "{{.ID}}",
)
PS_FORMAT = "\t".join(_FIELDS)


@dataclass(frozen=True)
class Container:
    project: str
    component: str  # Compose service name inside the project
    name: str
    state: str  # running, exited, restarting, created, paused, dead
    status: str  # human status, e.g. "Up 3 hours (healthy)"
    id: str

    @property
    def health(self) -> str | None:
        for marker, health in (
            ("(healthy)", "healthy"),
            ("(unhealthy)", "unhealthy"),
            ("(health: starting)", "starting"),
        ):
            if marker in self.status:
                return health
        return None


def parse_ps(text: str) -> list[Container]:
    containers = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == len(_FIELDS):
            containers.append(Container(*parts))
    return containers


def summarize(containers: Iterable[Container]) -> str:
    """One word-ish state for a set of long-running containers."""
    containers = list(containers)
    if not containers:
        return "absent"
    running = [c for c in containers if c.state == "running"]
    if any(c.health == "unhealthy" for c in running):
        return "unhealthy"
    if any(c.state == "restarting" for c in containers):
        return "restarting"
    if not running:
        return "stopped"
    if len(running) < len(containers):
        return f"{len(running)}/{len(containers)} running"
    if any(c.health == "starting" for c in running):
        return "starting"
    return "healthy" if all(c.health == "healthy" for c in running) else "running"


class Docker:
    """Engine access through sudo, without requiring docker-group membership."""

    def __init__(self, shell: Shell) -> None:
        self.shell = shell
        self._containers: list[Container] | None = None

    def containers(self, *, refresh: bool = False) -> list[Container]:
        if self._containers is None or refresh:
            out = self.shell.run(["docker", "ps", "--all", "--format", PS_FORMAT], sudo=True).stdout
            self._containers = parse_ps(out)
        return self._containers

    def project_containers(
        self, project: str, *, exclude: Iterable[str] = (), refresh: bool = False
    ) -> list[Container]:
        skipped = set(exclude)
        return [
            c
            for c in self.containers(refresh=refresh)
            if c.project == project and c.component not in skipped
        ]

    def image_ids(self, container_ids: Iterable[str]) -> dict[str, str]:
        """Map container ID to the image ID it runs."""
        ids = list(container_ids)
        if not ids:
            return {}
        out = self.shell.run(
            ["docker", "inspect", "--format", "{{.Id}} {{.Image}}", *ids], sudo=True
        ).stdout
        result = {}
        for line in out.splitlines():
            full_id, image_id = line.split()
            # docker ps prints short IDs; inspect prints full ones
            for cid in ids:
                if full_id.startswith(cid):
                    result[cid] = image_id
        return result

    def tag(self, source: str, target: str) -> None:
        self.shell.run(["docker", "image", "tag", source, target], sudo=True)

    def image_exists(self, ref: str) -> bool:
        return self.shell.run(["docker", "image", "inspect", ref], sudo=True, check=False).ok
