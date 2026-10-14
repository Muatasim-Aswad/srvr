"""Deploy snapshots: what a service runs before a change, and how to return to it.

`last-deploy.json` in the service's state directory holds the latest update's before/after
snapshots and backup, which `srvr rollback` uses."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import atomic, configsnapshots, deploymentprep, envfile
from .context import Context
from .docker import Container, summarize
from .errors import SrvrError
from .health import Probe, build_probe, probe
from .paths import Paths
from .registry import Service


@dataclass
class Snapshot:
    commit: str | None = None  # deployed Git commit (git services)
    images: dict[str, str] = field(default_factory=dict)  # compose service -> running image ID
    image_refs: dict[str, str] = field(default_factory=dict)  # compose service -> image reference
    compose_text: str | None = None  # primary compose file contents (image services)
    configuration: dict[str, dict[str, object]] | None = None
    display_refs: dict[str, str] | None = None

    def version(self) -> str:
        if self.commit:
            return self.commit[:9]
        return (
            ", ".join(
                envfile.display_value("IMAGE", ref)
                for ref in sorted(set((self.display_refs or self.image_refs).values()))
            )
            or "unknown"
        )


@dataclass
class DeployRecord:
    service: str
    time: str
    status: str  # started, ok, failed, rolled-back
    before: Snapshot
    after: Snapshot | None = None
    backup: str | None = None  # backup taken during this deploy
    recovery_required: bool = False

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
            recovery_required=raw.get("recovery_required", False),
        )


def _record_file(paths: Paths, service: str) -> Path:
    return paths.service_state(service) / "last-deploy.json"


def save_record(paths: Paths, record: DeployRecord) -> None:
    atomic.write_json(_record_file(paths, record.service), asdict(record))


def load_record(paths: Paths, service: str) -> DeployRecord | None:
    file = _record_file(paths, service)
    return DeployRecord.from_json(json.loads(file.read_text())) if file.exists() else None


def assert_recovered(paths: Paths, service: str) -> None:
    record = load_record(paths, service)
    journal = paths.service_state(service) / "configuration-recovery.json"
    if (
        journal.exists()
        or journal.is_symlink()
        or (record is not None and record.recovery_required)
    ):
        raise SrvrError(f"{service}: unfinished deployment recovery; recover with srvr rollback")


def _long_running(
    ctx: Context, service: Service, *, refresh: bool = True, timeout: float | None = None
) -> list[Container]:
    exclude = [service.migrate] if service.migrate else []
    return [
        container
        for container in ctx.docker.project_containers(
            service.project, exclude=exclude, refresh=refresh, timeout=timeout
        )
        if not container.one_off
    ]


def capture(ctx: Context, service: Service, *, preserve: bool = True) -> Snapshot:
    """Record the running version and keep its images from being pruned."""
    containers = _long_running(ctx, service)
    ids = ctx.docker.image_ids(c.id for c in containers)
    snapshot = Snapshot(
        commit=ctx.checkout(service).head() if service.is_git else None,
        images={c.component: ids[c.id] for c in containers if c.id in ids},
        image_refs=deploymentprep.compose(ctx, service).image_refs(),
        compose_text=None if service.is_git else service.compose_files[0].read_text(),
        configuration=configsnapshots.encode(
            configsnapshots.capture(
                service,
                extra=(configsnapshots.version_file(ctx.paths, service),),
            )
        ),
    )
    from .startup import migration_components

    runtime = ctx.paths.service_state(service.name) / "images.compose.json"
    if runtime.exists():
        jobs = migration_components(service, ctx.compose(service).config()["services"])
        pins = json.loads(runtime.read_text()).get("services", {})
        snapshot.images.update({name: pins[name]["image"] for name in jobs if name in pins})
    snapshot.display_refs = deploymentprep.display_refs(ctx, service, snapshot.image_refs)
    if preserve:
        preserve_images(ctx, service, snapshot)
    return snapshot


def preserve_images(
    ctx: Context, service: Service, snapshot: Snapshot, *, namespace: str = "srvr-prev"
) -> None:
    for component, image_id in snapshot.images.items():
        ctx.docker.tag(image_id, f"{namespace}/{service.project}-{component}:latest")


def apply(
    ctx: Context,
    service: Service,
    snapshot: Snapshot,
    *,
    expected_configuration: dict[str, dict[str, object]] | None = None,
) -> None:
    """Return the service to `snapshot`'s code and images and (re)start it."""
    if service.is_git and not snapshot.images:
        assert snapshot.commit
        with deploymentprep.Candidate(ctx, service, commit=snapshot.commit) as candidate:
            snapshot.images = candidate.prepare(
                stamp=ctx.checkout(candidate.compose.service).build_stamp()
            )
    if snapshot.configuration is not None:
        target = configsnapshots.decode(snapshot.configuration)
        expected = (
            configsnapshots.decode(expected_configuration)
            if expected_configuration is not None
            else configsnapshots.capture(
                service, extra=(configsnapshots.version_file(ctx.paths, service),)
            )
        )
        if service.is_git:
            # Repository files are restored by Git, never by a host configuration snapshot.
            tracked = ctx.shell.run(
                ["git", "-C", str(service.workdir), "ls-files", "-z"]
            ).stdout.split("\0")
            owned = {str(service.workdir / path) for path in tracked if path}
            target = {path: state for path, state in target.items() if path not in owned}
            expected = {path: state for path, state in expected.items() if path not in owned}
        configsnapshots.restore(ctx.paths, service, target, expected)
    compose = deploymentprep.compose(ctx, service)
    if service.is_git:
        assert snapshot.commit
        checkout = ctx.checkout(service)
        checkout.move_to(snapshot.commit)
    elif snapshot.configuration is None and snapshot.compose_text is not None:
        atomic.write_text(service.compose_files[0], snapshot.compose_text)
    compose.pin_images(snapshot.images)
    allowed = set(compose.config()["services"])
    orphan_ids = [
        container.id
        for container in ctx.docker.project_containers(service.project, refresh=True)
        if container.component not in allowed
    ]
    if orphan_ids:
        ctx.shell.run(["docker", "rm", "--force", *orphan_ids], sudo=True)
    from .startup import start_prepared

    start_prepared(ctx, service, migrate=False)


def after_data_restore(
    service: Service,
    expected: dict[str, dict[str, object]] | None,
) -> dict[str, dict[str, object]] | None:
    if expected is None:
        return None
    states = configsnapshots.decode(expected)
    for path in states:
        if any(Path(path).is_relative_to(data) for data in service.data):
            states[path] = configsnapshots.read(Path(path))
    return configsnapshots.encode(states)


def container_probe(
    ctx: Context,
    service: Service,
    *,
    expected: dict[str, Any] | None = None,
    timeout: float | None = None,
    require_healthy: bool = False,
) -> Probe:
    from .startup import migration_components

    expected = ctx.compose(service).config()["services"] if expected is None else expected
    jobs = migration_components(service, expected)
    expected = {name: spec for name, spec in expected.items() if name not in jobs}
    containers = [
        c for c in _long_running(ctx, service, timeout=timeout) if c.component in expected
    ]
    for name, spec in expected.items():
        replicas = spec.get("deploy", {}).get("replicas", spec.get("scale", 1))
        actual = [c for c in containers if c.component == name and not c.one_off]
        if len(actual) < replicas:
            return Probe(service.project, False, f"containers missing {name}")
        healthcheck = spec.get("healthcheck", {})
        if (
            require_healthy
            or (
                healthcheck
                and not healthcheck.get("disable")
                and healthcheck.get("test") != ["NONE"]
            )
        ) and any(c.state == "running" and c.health != "healthy" for c in actual):
            return Probe(service.project, False, f"containers health pending {name}")
    state = summarize(c for c in containers if not c.one_off)
    return Probe(service.project, state in ("healthy", "running"), f"containers {state}")


def wait_ready(
    ctx: Context,
    service: Service,
    *,
    timeout: float | None = None,
    components: set[str] | None = None,
    endpoints: bool = True,
    require_healthy: bool = False,
) -> Probe:
    """Require selected containers, HTTP and the source version within one deadline."""
    timeout = timeout if timeout is not None else ctx.settings.health_timeout
    deadline = time.monotonic() + timeout
    model = ctx.compose(service).config(timeout=max(0.01, timeout))["services"]
    if components is not None:
        model = {name: spec for name, spec in model.items() if name in components}
    revision = ctx.checkout(service).head() if endpoints and service.version_url else None
    successes = 0
    while True:
        result = container_probe(
            ctx,
            service,
            expected=model,
            timeout=max(0.01, deadline - time.monotonic()),
            require_healthy=require_healthy,
        )
        remaining = max(0.01, deadline - time.monotonic())
        if result.ok and endpoints and service.health:
            result = probe(service.health, timeout=min(5, remaining))
        if result.ok and revision and service.version_url:
            remaining = max(0.01, deadline - time.monotonic())
            result = build_probe(service.version_url, revision, timeout=min(5, remaining))
        successes = successes + 1 if result.ok else 0
        if successes >= 2:
            return result
        if result.ok:
            result = Probe(result.url, False, "readiness has not settled")
        if time.monotonic() >= deadline:
            return result
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return result
