"""`srvr prune`: reclaim disk space without touching anything a rollback needs."""

from __future__ import annotations

import shutil
from argparse import ArgumentParser, Namespace
from contextlib import ExitStack

from srvr.commands._shared import add_yes
from srvr.core import backups
from srvr.core.context import Context
from srvr.core.lock import service_lock
from srvr.core.ui import human_size

HELP = "Free disk space: untagged images, old build cache, backups beyond the keep limit"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--cache-age", default="168h", help="remove build cache unused for this long (default 168h)"
    )
    add_yes(parser)


def run(args: Namespace, ctx: Context) -> int:
    keep = ctx.settings.keep_backups
    ctx.ui.line(
        "Removes: untagged (dangling) images, build cache unused for "
        f"{args.cache_age}, and srvr backups beyond the newest {keep} per service."
    )
    ctx.ui.line("Keeps: running and tagged rollback images, plus each recorded rollback backup.")
    ctx.ui.confirm("Prune?", assume_yes=args.yes)
    ctx.shell.ensure_sudo()
    free_before = shutil.disk_usage("/").free

    with service_lock(ctx.paths, "_registry"), ExitStack() as locks:
        services = sorted(ctx.services([]), key=lambda service: service.name)
        for service in services:
            locks.enter_context(service_lock(ctx.paths, service.name))
        ctx.ui.step("images")
        ctx.shell.run(["docker", "image", "prune", "--force"], sudo=True, capture=False)
        ctx.ui.step("build cache")
        ctx.shell.run(
            ["docker", "builder", "prune", "--force", "--filter", f"until={args.cache_age}"],
            sudo=True,
            capture=False,
        )
        ctx.ui.step("backups")
        for service in services:
            removed = backups.prune(ctx.paths, service.name, keep=keep)
            if removed:
                ctx.ui.line(f"  {service.name}: removed {', '.join(b.name for b in removed)}")

    freed = shutil.disk_usage("/").free - free_before
    ctx.ui.ok(f"freed {human_size(max(freed, 0))}")
    return 0
