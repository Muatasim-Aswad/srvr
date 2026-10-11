"""`srvr backups`: list saved backups."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.core import backups as store
from srvr.core.context import Context
from srvr.core.ui import ago, human_size, parse_iso

HELP = "List a service's backups, newest first"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    saved = store.list_backups(ctx.paths, service.name)
    if not saved:
        ctx.ui.line(f"{service.name}: no backups; create one with `srvr backup {service.name}`")
        return 0
    rows = [
        [
            b.name,
            ago(parse_iso(b.meta["time"])) if "time" in b.meta else "-",
            human_size(b.size),
            b.meta.get("reason", "-"),
            b.meta.get("version", "-"),
        ]
        for b in saved
    ]
    ctx.ui.table(["NAME", "AGE", "SIZE", "REASON", "VERSION"], rows)
    ctx.ui.line(ctx.ui.paint(f"in {ctx.paths.service_backups(service.name)}", "dim"))
    return 0
