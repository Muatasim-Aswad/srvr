"""Execute prepared deployments under the service lock and recover failed switches."""

from __future__ import annotations

from dataclasses import dataclass

from . import backups, configsnapshots, deploymentprep, deploys, history
from .context import Context
from .deploymentplans import Plan, states
from .deploys import DeployRecord
from .errors import SrvrError
from .health import Probe
from .lock import service_lock
from .registry import Service
from .startup import migration_components, start_prepared
from .ui import human_size, iso, utc_now


@dataclass
class _Deployment:
    ctx: Context
    service: Service
    plan: Plan
    record: DeployRecord
    previous: DeployRecord | None
    taken: backups.Backup | None = None
    switch_started: bool = False

    def log(self, result: str, note: str = "") -> None:
        history.record(
            self.ctx.paths,
            self.service.name,
            "update",
            result,
            before=self.plan.current,
            after=self.plan.target,
            backup=self.record.backup or "",
            note=note,
        )

    def prepare(self) -> None:
        self.ctx.ui.step("preparing the new version (the current one keeps running)")
        try:
            self.plan.prepare()
            self.plan.validate()
        except BaseException as exc:
            self.plan.revert()
            deploymentprep.compose(self.ctx, self.service).pin_images(self.record.before.images)
            self.record.status = "failed"
            deploys.save_record(
                self.ctx.paths, self.previous if self.previous is not None else self.record
            )
            self.log("failed", "preparation failed (details hidden)")
            raise SrvrError(
                "preparation failed; current installation kept (details hidden)"
            ) from exc

    def switch(self, *, backup: bool) -> Probe:
        ctx, service, plan, record = self.ctx, self.service, self.plan, self.record
        compose = deploymentprep.compose(ctx, service)
        jobs = plan.migration_jobs or migration_components(service, compose.config()["services"])
        if jobs and service.has_data and not backup:
            raise SrvrError("migration deployments require a registered-data backup")
        plan.release_guard()
        record.recovery_required = True
        deploys.save_record(ctx.paths, record)
        self.switch_started = True
        deploys.preserve_images(ctx, service, record.before)
        ctx.ui.step("stopping for the prepared deployment")
        compose.stop()
        if backup and service.has_data:
            self.taken = backups.create(
                ctx.shell, ctx.paths, service, reason="update", version=record.before.version()
            )
            record.backup = self.taken.name
            deploys.save_record(ctx.paths, record)
            ctx.ui.ok(f"backup {self.taken.name} ({human_size(self.taken.size)})")
        if jobs:
            ctx.ui.warn(
                "only registered local data is recoverable; external databases and "
                "unregistered data need their own recovery plan"
            )
        plan.activate()
        compose = deploymentprep.compose(ctx, service)
        selected = plan.selected or (
            (plan.component,) if plan.component else tuple(record.before.images)
        )
        pins = plan.prepared_images or {
            name: image for name, image in record.before.images.items() if name not in selected
        }
        compose.pin_images(pins)
        record.after = deploys.Snapshot(
            commit=ctx.checkout(service).head() if service.is_git else None,
            images=dict(pins),
            image_refs=compose.image_refs(),
            configuration=configsnapshots.encode(states(ctx, service)),
        )
        deploys.save_record(ctx.paths, record)
        ctx.ui.step("starting the new version")
        start_prepared(ctx, service)
        ctx.ui.step("waiting for health")
        result = deploys.wait_ready(ctx, service)
        if not result.ok:
            ctx.ui.error(f"candidate is not healthy ({result.detail})")
            raise SrvrError("candidate is not healthy")
        return result

    def recover(self) -> None:
        self.ctx.ui.error(f"{self.service.name}: switch failed (details hidden)")
        if not self.switch_started:
            self.log("failed", "switch record was not published; current installation kept")
            return
        if self.record.after is None:
            try:
                self.plan.revert()
            except (SrvrError, OSError):
                self.record.status = "failed"
                deploys.save_record(self.ctx.paths, self.record)
                self.ctx.ui.error(
                    "configuration recovery needs administrator review (details hidden)"
                )
                self.log("failed", "configuration recovery pending")
                return
        roll_back(self.ctx, self.service, self.record, self.taken)
        self.log(self.record.status, "switch failed (details hidden)")

    def finish(self, result: Probe) -> None:
        ctx, service, record = self.ctx, self.service, self.record
        record.after = deploys.capture(ctx, service, preserve=False)
        record.status, record.recovery_required = "ok", False
        deploys.save_record(ctx.paths, record)
        configsnapshots.save_baseline(ctx.paths, service, states(ctx, service))
        self.log("ok")
        if self.taken:
            backups.prune(ctx.paths, service.name, keep=ctx.settings.keep_backups)
        ctx.ui.ok(f"{service.name}: {self.plan.current} → {self.plan.target} ({result.detail})")


def deploy(ctx: Context, service: Service, plan: Plan, *, backup: bool) -> bool:
    with service_lock(ctx.paths, service.name):
        deploys.assert_recovered(ctx.paths, service.name)
        plan.validate()
        ctx.shell.ensure_sudo()
        previous = deploys.load_record(ctx.paths, service.name)
        record = DeployRecord(
            service=service.name,
            time=iso(utc_now()),
            status="started",
            before=deploys.capture(ctx, service, preserve=False),
        )
        if plan.before_configuration is not None:
            record.before.configuration = configsnapshots.encode(plan.before_configuration)
        plan.before_images = dict(record.before.images)
        deploys.preserve_images(ctx, service, record.before, namespace="srvr-preparing")
        transaction = _Deployment(ctx, service, plan, record, previous)
        transaction.prepare()
        try:
            result = transaction.switch(backup=backup)
        except (SrvrError, OSError, KeyboardInterrupt):
            transaction.recover()
            return False
        transaction.finish(result)
        return True


def roll_back(
    ctx: Context, service: Service, record: DeployRecord, taken: backups.Backup | None
) -> None:
    """Automatic rollback after a failed switch; sets `record.status`."""
    ctx.ui.step(f"rolling back to {record.before.version()}")
    try:
        expected = record.after.configuration if record.after else None
        if expected is not None:
            configsnapshots.validate(configsnapshots.decode(expected))
        deploymentprep.compose(ctx, service).stop()
        if taken:
            kept = backups.restore(ctx.shell, service, taken)
            expected = deploys.after_data_restore(service, expected)
            ctx.ui.line(f"  data restored from {taken.name}; data from the failed run kept at:")
            for path in kept:
                ctx.ui.line(f"    {path}")
        deploys.apply(
            ctx,
            service,
            record.before,
            expected_configuration=expected,
        )
        result = deploys.wait_ready(ctx, service)
    except (SrvrError, OSError, KeyboardInterrupt):
        record.status = "failed"
        record.recovery_required = True
        ctx.ui.error(
            "rollback failed (details hidden). Previous images: "
            f"srvr-prev/{service.project}-*" + (f"; backup {taken.name}" if taken else "")
        )
    else:
        record.status = "rolled-back" if result.ok else "failed"
        record.recovery_required = not result.ok
        if result.ok:
            if record.before.configuration is not None:
                configsnapshots.save_baseline(ctx.paths, service, states(ctx, service))
            ctx.ui.warn(f"{service.name}: back on {record.before.version()} and healthy")
        else:
            ctx.ui.error(f"{service.name}: rolled back but not healthy ({result.detail})")
    deploys.save_record(ctx.paths, record)


def apply_rollback(
    ctx: Context,
    service: Service,
    record: DeployRecord,
    current_files: dict[str, configsnapshots.FileState],
    *,
    restore_data: bool,
) -> None:
    if (ctx.paths.service_state(service.name) / "configuration-recovery.json").exists():
        configsnapshots.recover(ctx.paths, service)
        current_files = states(ctx, service)
    record.recovery_required = True
    deploys.save_record(ctx.paths, record)
    deploymentprep.compose(ctx, service).stop()
    expected: dict[str, dict[str, object]] | None = configsnapshots.encode(current_files)
    if restore_data:
        assert record.backup
        backup = backups.find(ctx.paths, service.name, record.backup)
        for path in backups.restore(ctx.shell, service, backup):
            ctx.ui.line(f"  replaced data kept at {path}")
        expected = deploys.after_data_restore(service, expected)
    ctx.ui.step(f"switching to {record.before.version()}")
    deploys.apply(ctx, service, record.before, expected_configuration=expected)
