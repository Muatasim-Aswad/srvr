"""`srvr rollback`: undo the last successful `srvr update` of a service."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_yes, report_ready, version_of
from srvr.core import configsnapshots, configuration, deploys, history
from srvr.core.context import Context
from srvr.core.deploymenttransaction import apply_rollback
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

    current = "recovery pending" if record.recovery_required else version_of(ctx, service)
    current_files = configsnapshots.capture(
        service,
        extra=(configsnapshots.version_file(ctx.paths, service),),
    )
    runtime_file = ctx.paths.service_state(service.name) / "images.compose.json"
    runtime_state = configsnapshots.read(runtime_file)
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

    with service_lock(
        ctx.paths, service.name, allow_restore=args.data, allow_deployment_recovery=True
    ):
        configuration.validate(service, expected_configuration)
        if deploys.load_record(ctx.paths, service.name) != record:
            raise SrvrError(f"{service.name}: deployment record changed; rerun rollback")
        if not record.recovery_required and version_of(ctx, service) != current:
            raise SrvrError(f"{service.name}: running version changed; rerun rollback")
        configsnapshots.validate(current_files)
        configsnapshots.validate({str(runtime_file): runtime_state})
        if record.after and record.after.configuration and not record.recovery_required:
            configsnapshots.validate(configsnapshots.decode(record.after.configuration))
        ctx.shell.ensure_sudo()
        apply_rollback(ctx, service, record, current_files, restore_data=args.data)
        ok = report_ready(ctx, service)
        record.status = "rolled-back" if ok else "failed"
        record.recovery_required = not ok
        deploys.save_record(ctx.paths, record)
        if ok and record.before.configuration is not None:
            configsnapshots.save_baseline(
                ctx.paths,
                service,
                configsnapshots.capture(
                    service,
                    extra=(configsnapshots.version_file(ctx.paths, service),),
                ),
            )
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
