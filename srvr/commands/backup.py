"""`srvr backup`: consistent data backups, taken with the service briefly stopped."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import (
    add_services,
    chosen_services,
    report_ready,
    stopped,
    version_of,
)
from srvr.core import backups, configuration, history, volumes
from srvr.core.context import Context
from srvr.core.lock import service_lock
from srvr.core.registry import Service
from srvr.core.ui import human_size

HELP = "Back up services' data (each is stopped for a few seconds)"


def configure(parser: ArgumentParser) -> None:
    add_services(parser, require_choice=True)


def backup_one(ctx: Context, service: Service) -> None:
    expected = configuration.revision(service)
    with service_lock(ctx.paths, service.name):
        volumes.validate_registration(service, ctx.paths.services_dir)
        configuration.validate(service, expected)
        expected_volumes = volumes.inspect(ctx.shell, service, stopped=False)
        version = version_of(ctx, service)
        with stopped(ctx, service) as was_running:
            volumes.revalidate(ctx.shell, service, expected_volumes)
            ctx.ui.step(f"{service.name}: archiving {volumes.coverage(service)}")
            taken = backups.create(ctx.shell, ctx.paths, service, reason="manual", version=version)
        removed = backups.prune(ctx.paths, service.name, keep=ctx.settings.keep_backups)
        history.record(ctx.paths, service.name, "backup", "ok", before=version, backup=taken.name)
        if was_running:
            report_ready(ctx, service)
    ctx.ui.ok(f"{service.name}: {taken.name} ({human_size(taken.size)})")
    if removed:
        ctx.ui.line(f"  removed {len(removed)} old backup(s), keeping {ctx.settings.keep_backups}")


def run(args: Namespace, ctx: Context) -> int:
    services = chosen_services(ctx, args)
    ctx.shell.ensure_sudo()
    for service in services:
        if not service.has_data:
            ctx.ui.line(f"{service.name}: no data registered, skipped")
            continue
        backup_one(ctx, service)
    return 0
