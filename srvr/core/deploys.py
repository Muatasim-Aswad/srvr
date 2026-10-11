"""Deploy snapshots: what a service runs before a change, and how to return to it.

`last-deploy.json` in the service's state directory holds the latest update's before/after
snapshots and backup, which `srvr rollback` uses."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import atomic
from .context import Context
from .docker import Container, summarize
from .health import Probe, probe, wait_until
from .paths import Paths
from .registry import Service


@dataclass
class Snapshot:
    commit: str | None = None  # deployed Git commit (git services)
    images: dict[str, str] = field(default_factory=dict)  # compose service -> running image ID
    image_refs: dict[str, str] = field(default_factory=dict)  # compose service -> image reference
    compose_text: str | None = None  # primary compose file contents (image services)

    def version(self) -> str:
        if self.commit:
            return self.commit[:9]
        return ", ".join(sorted(set(self.image_refs.values()))) or "unknown"


@dataclass
class DeployRecord:
    service: str
    time: str
    status: str  # started, ok, failed, rolled-back
    before: Snapshot
    after: Snapshot | None = None
    backup: str | None = None  # backup taken during this deploy

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> DeployRecord:
        after = raw.get("after")
        return cls(
            service=raw["service"],
            time=raw["time"],
            status=raw["status"],
            before=Snapshot(**raw["before"]),
            after=Snapshot(**after) if after else None,
            backup=raw.get("backup"),
        )


def _record_file(paths: Paths, service: str) -> Path:
    return paths.service_state(service) / "last-deploy.json"


def save_record(paths: Paths, record: DeployRecord) -> None:
    atomic.write_json(_record_file(paths, record.service), asdict(record))


def load_record(paths: Paths, service: str) -> DeployRecord | None:
    file = _record_file(paths, service)
    return DeployRecord.from_json(json.loads(file.read_text())) if file.exists() else None


def _long_running(ctx: Context, service: Service, *, refresh: bool = True) -> list[Container]:
    exclude = [service.migrate] if service.migrate else []
    return ctx.docker.project_containers(service.project, exclude=exclude, refresh=refresh)


def capture(ctx: Context, service: Service, *, preserve: bool = True) -> Snapshot:
    """Record the running version and keep its images from being pruned."""
    containers = _long_running(ctx, service)
    ids = ctx.docker.image_ids(c.id for c in containers)
    snapshot = Snapshot(
        commit=ctx.checkout(service).head() if service.is_git else None,
        images={c.component: ids[c.id] for c in containers if c.id in ids},
        image_refs=ctx.compose(service).image_refs(),
        compose_text=None if service.is_git else service.compose_files[0].read_text(),
    )
    if preserve:
        preserve_images(ctx, service, snapshot)
    return snapshot


def preserve_images(
    ctx: Context, service: Service, snapshot: Snapshot, *, namespace: str = "srvr-prev"
) -> None:
    for component, image_id in snapshot.images.items():
        ctx.docker.tag(image_id, f"{namespace}/{service.project}-{component}:latest")


def apply(ctx: Context, service: Service, snapshot: Snapshot) -> None:
    """Return the service to `snapshot`'s code and images and (re)start it."""
    compose = ctx.compose(service)
    if service.is_git:
        assert snapshot.commit
        checkout = ctx.checkout(service)
        checkout.move_to(snapshot.commit)
        if not snapshot.images:
            compose.build(checkout.build_stamp())  # nothing was running before: rebuild it
    elif snapshot.compose_text is not None:
        atomic.write_text(service.compose_files[0], snapshot.compose_text)
    compose.pin_images(snapshot.images)
    compose.up(pull=False)


def container_probe(ctx: Context, service: Service) -> Probe:
    state = summarize(_long_running(ctx, service))
    return Probe(service.project, state in ("healthy", "running"), f"containers {state}")


def wait_ready(ctx: Context, service: Service, *, timeout: float | None = None) -> Probe:
    """Wait for the health URL, or for containers to run when no URL is registered."""
    timeout = timeout if timeout is not None else ctx.settings.health_timeout
    if service.health:
        url = service.health
        return wait_until(lambda: probe(url), timeout=timeout)
    return wait_until(lambda: container_probe(ctx, service), timeout=timeout)
