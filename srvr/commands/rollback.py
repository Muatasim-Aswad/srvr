"""`srvr rollback`: undo the last successful `srvr update` of a service."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_yes, report_ready, version_of
from srvr.core import backups, configuration, deploys, history
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock

HELP = "Return a service to the version before its last update"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")
    parser.add_argument(
        "--data", action="store_true", help="also restore the backup taken during that update"
    )
    add_yes(parser)


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    expected_configuration = configuration.revision(service)
    record = deploys.load_record(ctx.paths, service.name)
    if record is None:
        raise SrvrError(f"{service.name}: no update recorded by srvr, nothing to roll back to")
    if record.status == "rolled-back":
        raise SrvrError(f"{service.name}: the last update ({record.time}) was already rolled back")
    if args.data and not record.backup:
        raise SrvrError(f"{service.name}: that update took no backup, so --data is not possible")

    current = version_of(ctx, service)
    target = record.before.version()
    ctx.ui.fields(
        [
            ("service", service.name),
            ("update", f"{record.time} ({record.status})"),
            ("now", current),
            ("back to", target),
            ("data", f"restore {record.backup}" if args.data else "kept as it is"),
        ]
    )
    if args.data:
        ctx.ui.warn("changes made since that backup leave the live data (they are kept aside)")
    elif record.backup:
        ctx.ui.warn("if the update migrated data, the old version may need it back: add --data")
    ctx.ui.confirm(f"Roll back {service.name}?", assume_yes=args.yes)

    with service_lock(ctx.paths, service.name, allow_restore=args.data):
        configuration.validate(service, expected_configuration)
        if deploys.load_record(ctx.paths, service.name) != record:
            raise SrvrError(f"{service.name}: deployment record changed; rerun rollback")
        if version_of(ctx, service) != current:
            raise SrvrError(f"{service.name}: running version changed; rerun rollback")
        ctx.shell.ensure_sudo()
        if args.data:
            assert record.backup
            backup = backups.find(ctx.paths, service.name, record.backup)
            ctx.compose(service).stop()
            for path in backups.restore(ctx.shell, service, backup):
                ctx.ui.line(f"  replaced data kept at {path}")
        ctx.ui.step(f"switching to {target}")
        deploys.apply(ctx, service, record.before)
        ok = report_ready(ctx, service)
        record.status = "rolled-back" if ok else "failed"
        deploys.save_record(ctx.paths, record)
        history.record(
            ctx.paths,
            service.name,
            "rollback",
            "ok" if ok else "failed",
            before=current,
            after=target,
            backup=record.backup if args.data and record.backup else "",
        )
    return 0 if ok else 1
