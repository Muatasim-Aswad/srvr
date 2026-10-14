"""`srvr snapshot`: plain data copies for the off-server backup, taken while briefly stopped."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path

from srvr.commands._shared import add_services, chosen_services, report_ready, stopped
from srvr.core import configuration, history, snapshots, volumes
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.registry import Service

HELP = "Copy services' data for the off-server backup (each is stopped for a few seconds)"


def configure(parser: ArgumentParser) -> None:
    add_services(parser, require_choice=True)
    parser.add_argument(
        "--to",
        required=True,
        type=Path,
        metavar="DIR",
        help="staging directory; each service's copy goes in DIR/<service>",
    )


def snapshot_one(ctx: Context, service: Service, dest: Path) -> bool:
    """True when the copy was refreshed and the service is back up."""
    expected = configuration.revision(service)
    with service_lock(ctx.paths, service.name):
        volumes.validate_registration(service, ctx.paths.services_dir)
        configuration.validate(service, expected)
        expected_volumes = volumes.inspect(ctx.shell, service, stopped=False)
        try:
            with stopped(ctx, service) as was_running:
                volumes.revalidate(ctx.shell, service, expected_volumes)
                ctx.ui.step(f"{service.name}: copying {volumes.coverage(service)}")
                copied = snapshots.copy(ctx.shell, service, dest)
        except SrvrError as exc:
            history.record(ctx.paths, service.name, "snapshot", "failed", note=str(exc))
            ctx.ui.error(f"{service.name}: {exc}; check staging for the previous copy")
            return False
        history.record(ctx.paths, service.name, "snapshot", "ok", note=str(copied))
        ctx.ui.ok(f"{service.name}: {copied}")
        return report_ready(ctx, service) if was_running else True


def run(args: Namespace, ctx: Context) -> int:
    services = chosen_services(ctx, args)
    dest = args.to.absolute()
    ctx.shell.ensure_sudo()
    ok = True
    for service in services:
        if not service.has_data:
            ctx.ui.line(f"{service.name}: no data registered, skipped")
            continue
        ok = snapshot_one(ctx, service, dest) and ok
    return 0 if ok else 1
