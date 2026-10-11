"""`srvr logs`: container logs through Compose."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.core.context import Context

HELP = "Show a service's container logs"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")
    parser.add_argument("component", nargs="?", help="one Compose service inside it (default: all)")
    parser.add_argument(
        "-n", "--tail", type=int, default=100, help="lines per container (default 100)"
    )
    parser.add_argument("-f", "--follow", action="store_true", help="keep streaming new lines")
    parser.add_argument("--since", help="only newer lines, e.g. 30m or 2026-09-29T10:00")


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    ctx.shell.ensure_sudo()
    ctx.compose(service).logs(
        tail=args.tail, follow=args.follow, since=args.since, component=args.component
    )
    return 0
