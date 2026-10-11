"""`srvr history`: what srvr has changed, newest first."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_services
from srvr.core import history
from srvr.core.context import Context

HELP = "Show recorded updates, rollbacks, restores and backups"


def configure(parser: ArgumentParser) -> None:
    add_services(parser)
    parser.add_argument("-n", "--limit", type=int, default=20, help="rows to show (default 20)")


def run(args: Namespace, ctx: Context) -> int:
    events = [e for s in ctx.services(args.services) for e in history.read(ctx.paths, s.name)]
    events.sort(key=lambda e: e.time, reverse=True)
    if not events:
        ctx.ui.line("no recorded changes yet")
        return 0
    rows = [
        [
            e.time.replace("T", " ").removesuffix("Z"),
            e.service,
            e.action,
            ctx.ui.state(e.result),
            e.before or "-",
            e.after or "-",
            e.backup or "-",
            e.note,
        ]
        for e in events[: args.limit]
    ]
    ctx.ui.table(
        ["TIME (UTC)", "SERVICE", "ACTION", "RESULT", "FROM", "TO", "BACKUP", "NOTE"], rows
    )
    return 0
