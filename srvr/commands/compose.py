"""`srvr compose`: any Docker Compose command with the service's project, env and overrides."""

from __future__ import annotations

import argparse
from argparse import ArgumentParser, Namespace

from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock

HELP = "Run a raw Docker Compose command for a service (with sudo and its registered files)"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")
    parser.add_argument(
        "compose_args",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="Compose arguments, e.g. `ps`, `logs -f api`, `config`, `exec api sh`",
    )


def refuse_destructive(args: list[str]) -> None:
    """Never let `down` delete volumes; that can destroy data or rollback copies."""
    if args and args[0] == "down" and any(a in ("-v", "--volumes") for a in args[1:]):
        raise SrvrError("refusing `down --volumes`: it can delete persistent data")


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    compose_args = list(args.compose_args)
    if compose_args[:1] == ["--"]:
        compose_args = compose_args[1:]
    if not compose_args:
        raise SrvrError(f"give Compose arguments, e.g. `srvr compose {service.name} ps`")
    refuse_destructive(compose_args)
    ctx.shell.ensure_sudo()
    if compose_args[0] in (
        "ps",
        "ls",
        "logs",
        "config",
        "images",
        "version",
        "top",
        "port",
        "events",
    ):
        return ctx.compose(service).passthrough(compose_args)
    with service_lock(ctx.paths, service.name):
        return ctx.compose(service).passthrough(compose_args)
