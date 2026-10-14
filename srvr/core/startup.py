"""Prepared startup and one-off migration prerequisites."""

from __future__ import annotations

from typing import Any

from .context import Context
from .errors import SrvrError
from .onboarding import selected_services
from .registry import Service


def migration_components(service: Service, specs: dict[str, Any]) -> set[str]:
    jobs = {service.migrate} if service.migrate else set()
    jobs.update(
        dependency
        for spec in specs.values()
        for dependency, options in spec.get("depends_on", {}).items()
        if dependency in specs and options.get("condition") == "service_completed_successfully"
    )
    return jobs


def validate_dependencies(service: Service, model: dict[str, Any]) -> None:
    specs = selected_services(service, model)
    if service.migrate:
        specs[service.migrate] = model["services"][service.migrate]
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting or name not in specs:
            raise SrvrError("dependencies are cyclic or outside registered profiles")
        if name in visited:
            return
        visiting.add(name)
        for dependency, options in specs[name].get("depends_on", {}).items():
            if dependency not in specs and not options.get("required", True):
                continue
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in specs:
        visit(name)


def start_prepared(ctx: Context, service: Service, *, migrate: bool = True) -> None:
    from . import deploys

    compose = ctx.compose(service)
    model = compose.config(all_profiles=True)
    specs = selected_services(service, model)
    if service.migrate:
        specs[service.migrate] = model["services"][service.migrate]
    jobs = migration_components(service, specs)
    healthy = {
        dependency
        for spec in specs.values()
        for dependency, options in spec.get("depends_on", {}).items()
        if options.get("condition") == "service_healthy"
    }
    if not jobs:
        compose.up(pull=False)
        return
    started: set[str] = set()
    visiting: set[str] = set()

    def start(name: str) -> None:
        if name in started:
            return
        if name in visiting or name not in specs:
            raise SrvrError("migration dependencies are cyclic or outside registered profiles")
        visiting.add(name)
        for dependency, options in specs[name].get("depends_on", {}).items():
            if dependency not in specs and not options.get("required", True):
                continue
            start(dependency)
        if name in jobs:
            if migrate:
                ctx.ui.step(f"running {name}")
                compose.run_once(name, pull=False, timeout=ctx.settings.migration_timeout)
        else:
            compose.up_component(name)
            result = deploys.wait_ready(
                ctx,
                service,
                components={name},
                endpoints=False,
                require_healthy=name in healthy,
            )
            if not result.ok:
                raise SrvrError(f"{name}: migration/startup prerequisite is not ready")
        visiting.remove(name)
        started.add(name)

    for name in sorted(jobs):
        start(name)
    for name in specs:
        start(name)
