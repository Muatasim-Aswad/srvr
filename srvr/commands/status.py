"""`srvr status`: one line per service."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from concurrent.futures import ThreadPoolExecutor

from srvr.commands._shared import add_services, docker_access, last_event, version_of
from srvr.core.context import Context
from srvr.core.docker import summarize
from srvr.core.errors import SrvrError
from srvr.core.health import Probe, probe
from srvr.core.registry import Service

HELP = "Overview of every service (the default command)"


def configure(parser: ArgumentParser) -> None:
    add_services(parser)
    parser.add_argument(
        "--no-sudo", action="store_true", help="skip container state rather than ask for sudo"
    )


def _health(service: Service) -> Probe | None:
    return probe(service.health) if service.health else None


def _version(ctx: Context, service: Service) -> str:
    try:
        return version_of(ctx, service)
    except SrvrError as exc:
        return f"error: {exc}"


def run(args: Namespace, ctx: Context) -> int:
    services = ctx.services(args.services)
    with_docker = docker_access(ctx, prompt=not args.no_sudo)
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(_health, services))

    rows = []
    for service, result in zip(services, probes, strict=True):
        if with_docker:
            exclude = [service.migrate] if service.migrate else []
            state = summarize(ctx.docker.project_containers(service.project, exclude=exclude))
        else:
            state = "?"
        health = "-" if result is None else ("ok" if result.ok else "fail")
        rows.append(
            [
                service.name,
                ctx.ui.state(state),
                ctx.ui.state(health),
                _version(ctx, service),
                last_event(ctx, service),
                service.url or "-",
            ]
        )
    ctx.ui.table(["SERVICE", "CONTAINERS", "HEALTH", "VERSION", "LAST CHANGE", "URL"], rows)
    if not with_docker:
        ctx.ui.warn("container state skipped (needs sudo)")
    failing = [s.name for s, r in zip(services, probes, strict=True) if r and not r.ok]
    return 1 if failing else 0
