"""`srvr stop`: stop containers, keeping them and all data."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_services, chosen_services
from srvr.core.context import Context
from srvr.core.lock import service_lock

HELP = "Stop services (containers and data are kept)"


def configure(parser: ArgumentParser) -> None:
    add_services(parser, require_choice=True)


def run(args: Namespace, ctx: Context) -> int:
    services = chosen_services(ctx, args)
    ctx.shell.ensure_sudo()
    for service in services:
        with service_lock(ctx.paths, service.name, allow_restore=True):
            ctx.ui.step(f"{service.name}: stopping")
            ctx.compose(service).stop()
            ctx.ui.ok(f"{service.name}: stopped; `srvr start {service.name}` brings it back")
    return 0
