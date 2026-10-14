"""`srvr restore`: put a backup back in place of the live data."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path

from srvr.commands._shared import add_yes, report_ready, version_of
from srvr.core import backups, configuration, history, snapshots, volumes
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.registry import Service
from srvr.core.ui import human_size

HELP = "Replace a service's data with a backup (current data is moved aside, not deleted)"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")
    parser.add_argument("backup", nargs="?", default="latest", help="backup name (default: latest)")
    parser.add_argument(
        "--snapshot",
        type=Path,
        metavar="DIR",
        help="restore a service's off-server snapshot directory",
    )
    add_yes(parser)


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    with service_lock(ctx.paths, service.name, allow_restore=True):
        return _restore(args, ctx, service)


def _restore(args: Namespace, ctx: Context, service: Service) -> int:
    volumes.validate_registration(service, ctx.paths.services_dir)
    ctx.shell.ensure_sudo()
    expected_volumes = volumes.inspect(ctx.shell, service, stopped=False)
    expected_configuration = configuration.revision(service)
    selected = _source(args, ctx, service)
    if isinstance(selected, backups.Backup):
        name = selected.name
        description = f"{name} ({human_size(selected.size)})"
    else:
        name = str(selected.path)
        description = name
    current = version_of(ctx, service)
    ctx.ui.fields(
        [
            ("service", service.name),
            ("backup", description),
            ("made by", selected.meta.get("version", "unknown version")),
            ("running", current),
            ("replaces", volumes.coverage(service)),
        ]
    )
    made_by = selected.meta.get("version")
    if made_by and made_by not in current and current not in made_by:
        ctx.ui.warn("the backup comes from a different version than the one running")
    ctx.ui.warn("the service stops, and changes made since the backup leave the live data")
    ctx.ui.confirm(f"Restore {name} into {service.name}?", assume_yes=args.yes)

    configuration.validate(service, expected_configuration)
    volumes.validate_registration(service, ctx.paths.services_dir)
    if volumes.inspect(ctx.shell, service, stopped=False) != expected_volumes:
        raise SrvrError(f"{service.name}: volume identity changed; rerun restore")
    _validate_source(ctx, service, selected)
    if version_of(ctx, service) != current:
        raise SrvrError(f"{service.name}: running version changed; rerun restore")
    return _switch(
        ctx, service, selected, expected_volumes=expected_volumes, current=current, name=name
    )


def _source(args: Namespace, ctx: Context, service: Service) -> backups.Backup | snapshots.Snapshot:
    source = getattr(args, "snapshot", None)
    if source is not None:
        if args.backup != "latest":
            raise SrvrError("choose a local backup or --snapshot, not both")
        return snapshots.read(ctx.shell, service, source)
    return backups.find(ctx.paths, service.name, args.backup)


def _switch(
    ctx: Context,
    service: Service,
    selected: backups.Backup | snapshots.Snapshot,
    *,
    expected_volumes: tuple[volumes.Volume, ...],
    current: str,
    name: str,
) -> int:
    compose = ctx.compose(service)
    ctx.ui.step(f"{service.name}: stopping")
    compose.stop()
    ctx.ui.step(f"{service.name}: restoring {name}")
    journal = ctx.paths.service_backups(service.name) / ".restore.json"
    try:
        volumes.revalidate(ctx.shell, service, expected_volumes)
        if isinstance(selected, backups.Backup):
            kept = backups.restore(ctx.shell, service, selected)
        else:
            kept = snapshots.restore(ctx.shell, service, selected, journal)
    except (SrvrError, OSError, KeyboardInterrupt):
        if journal.exists() or journal.is_symlink():
            ctx.ui.error("restore recovery is unfinished; service remains stopped; retry restore")
        else:
            volumes.revalidate(ctx.shell, service, expected_volumes)
            ctx.ui.error("restore failed; previous data recovered, starting it again")
            compose.up(pull=False)
        raise
    ctx.ui.step(f"{service.name}: starting")
    compose.up(pull=False)
    ok = report_ready(ctx, service)
    history.record(
        ctx.paths,
        service.name,
        "restore",
        "ok" if ok else "failed",
        before=current,
        backup=selected.name if isinstance(selected, backups.Backup) else "",
        note=f"snapshot {name}" if isinstance(selected, snapshots.Snapshot) else "",
    )
    for path in kept:
        ctx.ui.line(f"  previous data kept at {path} (delete it once you are satisfied)")
    return 0 if ok else 1


def _validate_source(
    ctx: Context, service: Service, selected: backups.Backup | snapshots.Snapshot
) -> None:
    if isinstance(selected, backups.Backup):
        changed = backups.find(ctx.paths, service.name, selected.name).meta != selected.meta
    else:
        changed = snapshots.read(ctx.shell, service, selected.path) != selected
    if changed:
        raise SrvrError(f"{service.name}: backup changed; rerun restore")
