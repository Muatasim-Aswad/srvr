"""`srvr start`: bring services up without building."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_services, chosen_services, report_ready
from srvr.core.context import Context
from srvr.core.lock import service_lock

HELP = "Start services; also applies changed Compose or env files"


def configure(parser: ArgumentParser) -> None:
    add_services(parser, require_choice=True)


def run(args: Namespace, ctx: Context) -> int:
    services = chosen_services(ctx, args)
    ctx.shell.ensure_sudo()
    failed = 0
    for service in services:
        with service_lock(ctx.paths, service.name):
            ctx.ui.step(f"{service.name}: starting")
            ctx.compose(service).up()
            if not report_ready(ctx, service):
                failed += 1
    return 1 if failed else 0
