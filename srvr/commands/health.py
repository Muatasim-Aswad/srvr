"""`srvr health`: probe loopback and private HTTPS health endpoints, and the served build."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from srvr.commands._shared import add_services
from srvr.core.context import Context
from srvr.core.deploys import container_probe
from srvr.core.health import Probe, build_probe, probe

HELP = "Check selected containers, loopback/private HTTPS endpoints, and the served build"


def configure(parser: ArgumentParser) -> None:
    add_services(parser)


def run(args: Namespace, ctx: Context) -> int:
    services = ctx.services(args.services)
    if services:
        ctx.shell.ensure_sudo()
    checks: list[tuple[str, str, str, Callable[[], Probe]]] = [
        (service.name, "containers", service.project, partial(container_probe, ctx, service))
        for service in services
    ]
    checks += [
        (service.name, label, url, partial(probe, url))
        for service in services
        for label, url in (("local", service.health), ("https", service.url))
        if url
    ]
    checks += [
        (
            s.name,
            "build",
            s.version_url,
            partial(build_probe, s.version_url, ctx.checkout(s).head()),
        )
        for s in services
        if s.version_url
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda check: check[3](), checks))

    rows = [
        [name, label, ctx.ui.state("ok" if r.ok else "fail"), r.detail, url]
        for (name, label, url, _), r in zip(checks, results, strict=True)
    ]
    ctx.ui.table(["SERVICE", "CHECK", "RESULT", "DETAIL", "URL"], rows)
    return 0 if all(r.ok for r in results) else 1
