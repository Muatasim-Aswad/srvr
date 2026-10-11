"""`srvr restore`: put a backup back in place of the live data."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_yes, report_ready, version_of
from srvr.core import backups, configuration, history
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.ui import human_size

HELP = "Replace a service's data with a backup (current data is moved aside, not deleted)"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")
    parser.add_argument("backup", nargs="?", default="latest", help="backup name (default: latest)")
    add_yes(parser)


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    expected_configuration = configuration.revision(service)
    backup = backups.find(ctx.paths, service.name, args.backup)
    current = version_of(ctx, service)
    ctx.ui.fields(
        [
            ("service", service.name),
            ("backup", f"{backup.name} ({human_size(backup.size)})"),
            ("made by", backup.meta.get("version", "unknown version")),
            ("running", current),
            ("replaces", ", ".join(str(d) for d in service.data)),
        ]
    )
    made_by = backup.meta.get("version")
    if made_by and made_by not in current and current not in made_by:
        ctx.ui.warn("the backup comes from a different version than the one running")
    ctx.ui.warn("the service stops, and changes made since the backup leave the live data")
    ctx.ui.confirm(f"Restore {backup.name} into {service.name}?", assume_yes=args.yes)

    with service_lock(ctx.paths, service.name, allow_restore=True):
        configuration.validate(service, expected_configuration)
        if backups.find(ctx.paths, service.name, backup.name).meta != backup.meta:
            raise SrvrError(f"{service.name}: backup changed; rerun restore")
        if version_of(ctx, service) != current:
            raise SrvrError(f"{service.name}: running version changed; rerun restore")
        ctx.shell.ensure_sudo()
        compose = ctx.compose(service)
        ctx.ui.step(f"{service.name}: stopping")
        compose.stop()
        ctx.ui.step(f"{service.name}: restoring {backup.name}")
        try:
            kept = backups.restore(ctx.shell, service, backup)
        except (SrvrError, OSError, KeyboardInterrupt):
            if (backup.path.parent / ".restore.json").exists():
                ctx.ui.error(
                    "restore recovery is unfinished; service remains stopped; retry restore"
                )
            else:
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
            backup=backup.name,
        )
    for path in kept:
        ctx.ui.line(f"  previous data kept at {path} (delete it once you are satisfied)")
    return 0 if ok else 1
