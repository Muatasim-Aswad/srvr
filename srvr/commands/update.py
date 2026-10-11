"""`srvr update`: deploy a new version with backup, verification and automatic rollback.

Order of work, chosen to keep downtime short and every step reversible:
  1. plan     resolve the target (Git ref or pinned image) and show what changes
  2. prepare  move the checkout / edit the image pin, then build or pull; the old version
              keeps serving, and a failure here reverts the checkout or pin
  3. switch   stop, back up the data, run the migrate service, start, wait for health
  4. verify   on any failure in step 3, restore the backup and the previous images and code
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field

from srvr.commands._shared import add_yes, chosen_services
from srvr.core import atomic, backups, configuration, deploys, history
from srvr.core.composefile import find_image, set_image, validate_image_ref
from srvr.core.context import Context
from srvr.core.deploys import DeployRecord
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.registry import Service
from srvr.core.ui import human_size, iso, utc_now

HELP = "Deploy a new version: back up, build or pull, migrate, restart, verify"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("services", nargs="*", metavar="SERVICE", help="service(s) to update")
    parser.add_argument("--all", action="store_true", help="every service (useful with --check)")
    parser.add_argument("--check", action="store_true", help="only show what would change")
    parser.add_argument(
        "--ref", help="git services: branch, tag or commit (default: registered branch)"
    )
    parser.add_argument(
        "--image", help="image services: exact new image, e.g. name:1.2.3[@sha256:...]"
    )
    parser.add_argument("--component", help="image services: Compose component to update")
    parser.add_argument(
        "--force", action="store_true", help="redeploy even if already at the target"
    )
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-update data backup")
    add_yes(parser)


@dataclass
class Plan:
    current: str
    target: str
    prepare: Callable[[], None]  # make the target version ready to start
    revert: Callable[[], None]  # undo `prepare` before anything was switched
    commits: list[str] = field(default_factory=list)
    warning: str | None = None
    component: str | None = None
    validate: Callable[[], None] = lambda: None


def plan_git(ctx: Context, service: Service, args: Namespace) -> Plan | None:
    expected_configuration = configuration.revision(service)
    checkout = ctx.checkout(service)
    if not checkout.is_clean():
        raise SrvrError(f"{service.name}: {service.workdir} has local changes; resolve them first")
    assert service.branch
    ctx.ui.step(f"{service.name}: fetching origin/{service.branch}")
    if not checkout.fetch(service.branch):
        raise SrvrError(f"{service.name}: cannot fetch origin/{service.branch}")
    if args.ref and args.ref != service.branch:
        checkout.fetch(args.ref)  # succeeds only if the ref is a branch; tags came with the fetch

    current = checkout.head()
    target = checkout.resolve(args.ref or service.branch)
    if target == current and not args.force:
        ctx.ui.ok(f"{service.name}: up to date at {checkout.describe(current)}")
        return None
    forward = checkout.is_ancestor(current, target)
    if not forward and not args.ref:
        raise SrvrError(
            f"{service.name}: origin/{service.branch} is not ahead of the deployed commit; "
            "use --ref to deploy it deliberately"
        )
    compose = ctx.compose(service)

    def validate() -> None:
        configuration.validate(service, expected_configuration)
        if checkout.head() != current or not checkout.is_clean():
            raise SrvrError(f"{service.name}: checkout changed; rerun the update")

    def prepare() -> None:
        checkout.move_to(target)
        compose.build(checkout.build_stamp())

    return Plan(
        current=checkout.describe(current),
        target=checkout.describe(target),
        prepare=prepare,
        revert=lambda: checkout.move_to(current),
        commits=checkout.log(current, target) if forward else [],
        warning=None if forward else "the target is not ahead of the current commit",
        validate=validate,
    )


def plan_image(ctx: Context, service: Service, args: Namespace, *, named: bool) -> Plan | None:
    """`named`: the user asked for exactly this service, so a missing --image is an error."""
    expected_configuration = configuration.revision(service)
    file = service.compose_files[0]
    original = file.read_text()
    compose = ctx.compose(service)
    model = compose.config()
    components = [
        name for name, spec in model["services"].items() if "build" not in spec and "image" in spec
    ]
    component = getattr(args, "component", None) or service.update_component
    if component is not None and component not in components:
        raise SrvrError(f"{service.name}: {component!r} is not an image component")
    if not args.image:
        current = ", ".join(
            f"{name}={model['services'][name]['image']}"
            for name in ([component] if component else components)
        )
        if args.check:
            ctx.ui.line(f"{service.name}: pinned to {current}; pass --image to plan an upgrade")
            return None
        if not named:
            ctx.ui.line(f"{service.name}: pinned to {current}; skipped (upgrade with --image)")
            return None
        raise SrvrError(
            f"{service.name}: pass --image <exact reference>; srvr never picks a version"
        )
    if component is None:
        if len(components) != 1:
            raise SrvrError(f"{service.name}: choose --component from {', '.join(components)}")
        component = components[0]
    _, current = find_image(original, component)
    if current != model["services"][component]["image"] or "$" in current:
        raise SrvrError(
            f"{service.name}: {component}'s image must be a literal in {file}; "
            "resolve environment or override image settings first"
        )
    validate_image_ref(args.image)
    if args.image == current and not args.force:
        ctx.ui.ok(f"{service.name}: already pinned to {current}")
        return None
    updated = set_image(original, component, args.image)
    expected = deepcopy(model)
    expected["services"][component]["image"] = args.image

    def validate() -> None:
        configuration.validate(service, expected_configuration)
        if file.read_text() != original or compose.config() != model:
            raise SrvrError(f"{service.name}: Compose configuration changed; rerun the update")

    def prepare() -> None:
        atomic.write_text(file, updated)
        if compose.config() != expected:
            raise SrvrError(
                f"{service.name}: image edit did not change only {component}; "
                "check Compose overrides and shared image definitions"
            )
        compose.pull(component)

    def revert() -> None:
        atomic.write_text(file, original)

    return Plan(
        current=current,
        target=args.image,
        prepare=prepare,
        revert=revert,
        warning="read the upstream release notes before continuing",
        component=component,
        validate=validate,
    )


def show(ctx: Context, service: Service, plan: Plan) -> None:
    ctx.ui.line(f"{ctx.ui.paint(service.name, 'bold')}: {plan.current} → {plan.target}")
    for commit in plan.commits:
        ctx.ui.line(f"  {commit}")
    if plan.warning:
        ctx.ui.warn(plan.warning)


def deploy(ctx: Context, service: Service, plan: Plan, *, backup: bool) -> bool:
    with service_lock(ctx.paths, service.name):
        plan.validate()
        ctx.shell.ensure_sudo()
        compose = ctx.compose(service)
        previous = deploys.load_record(ctx.paths, service.name)
        record = DeployRecord(
            service=service.name,
            time=iso(utc_now()),
            status="started",
            before=deploys.capture(ctx, service, preserve=False),
        )
        deploys.preserve_images(ctx, service, record.before, namespace="srvr-preparing")
        deploys.save_record(ctx.paths, record)

        def log(result: str, note: str = "") -> None:
            history.record(
                ctx.paths,
                service.name,
                "update",
                result,
                before=plan.current,
                after=plan.target,
                backup=record.backup or "",
                note=note,
            )

        ctx.ui.step("preparing the new version (the current one keeps running)")
        try:
            plan.prepare()
        except BaseException as exc:
            plan.revert()
            compose.pin_images(record.before.images)
            record.status = "failed"
            deploys.save_record(ctx.paths, previous if previous is not None else record)
            log("failed", f"prepare: {exc}")
            raise

        taken = None
        try:
            deploys.preserve_images(ctx, service, record.before)
            compose.pin_images(
                {
                    name: image
                    for name, image in record.before.images.items()
                    if name != plan.component
                }
                if plan.component is not None
                else {}
            )
            if backup and service.data:
                ctx.ui.step("stopping for a consistent backup")
                compose.stop()
                taken = backups.create(
                    ctx.shell, ctx.paths, service, reason="update", version=record.before.version()
                )
                record.backup = taken.name
                deploys.save_record(ctx.paths, record)
                ctx.ui.ok(f"backup {taken.name} ({human_size(taken.size)})")
            if service.migrate:
                ctx.ui.step(f"running {service.migrate}")
                compose.run_once(service.migrate, pull=service.is_git)
            ctx.ui.step("starting the new version")
            compose.up(pull=service.is_git)
            ctx.ui.step("waiting for health")
            result = deploys.wait_ready(ctx, service)
            if not result.ok:
                raise SrvrError(f"not healthy: {result.detail}")
        except (SrvrError, OSError, KeyboardInterrupt) as exc:
            ctx.ui.error(f"{service.name}: {exc or 'interrupted'}")
            roll_back(ctx, service, record, taken)
            log(record.status, str(exc))
            return False

        record.after, record.status = deploys.capture(ctx, service, preserve=False), "ok"
        deploys.save_record(ctx.paths, record)
        log("ok")
        if taken:
            backups.prune(ctx.paths, service.name, keep=ctx.settings.keep_backups)
        ctx.ui.ok(f"{service.name}: {plan.current} → {plan.target} ({result.detail})")
        return True


def roll_back(
    ctx: Context, service: Service, record: DeployRecord, taken: backups.Backup | None
) -> None:
    """Automatic rollback after a failed switch; sets `record.status`."""
    ctx.ui.step(f"rolling back to {record.before.version()}")
    try:
        if taken:
            ctx.compose(service).stop()
            kept = backups.restore(ctx.shell, service, taken)
            ctx.ui.line(f"  data restored from {taken.name}; data from the failed run kept at:")
            for path in kept:
                ctx.ui.line(f"    {path}")
        deploys.apply(ctx, service, record.before)
        result = deploys.wait_ready(ctx, service)
    except (SrvrError, OSError) as exc:
        record.status = "failed"
        ctx.ui.error(
            f"rollback failed: {exc}. Previous images are tagged srvr-prev/{service.project}-*"
            + (f"; backup {taken.name}" if taken else "")
        )
    else:
        record.status = "rolled-back" if result.ok else "failed"
        if result.ok:
            ctx.ui.warn(f"{service.name}: back on {record.before.version()} and healthy")
        else:
            ctx.ui.error(f"{service.name}: rolled back but not healthy ({result.detail})")
    deploys.save_record(ctx.paths, record)


def update_order(services: list[Service]) -> list[Service]:
    """Git services first, then image services (usually just listed as pinned), each group in
    its given order."""
    return sorted(services, key=lambda service: not service.is_git)


def validate_selection(args: Namespace, services: list[Service]) -> None:
    component = getattr(args, "component", None)
    if (args.ref or args.image or component) and len(services) != 1:
        raise SrvrError("--ref, --image and --component apply to exactly one service")
    if args.ref and any(not s.is_git for s in services):
        raise SrvrError("--ref is for git services; use --image for image services")
    if args.image and any(s.is_git for s in services):
        raise SrvrError("--image is for image services; use --ref for git services")
    if component and any(s.is_git for s in services):
        raise SrvrError(
            "--component is for image services; Git updates use the repository's Compose"
        )


def run(args: Namespace, ctx: Context) -> int:
    services = (
        ctx.services([])
        if args.check and not args.services and not args.all
        else chosen_services(ctx, args)
    )
    validate_selection(args, services)
    named = len(services) == 1 and not args.all
    for service in update_order(services):
        if len(services) > 1:
            ctx.ui.section(service.name)
        if service.is_git:
            plan = plan_git(ctx, service, args)
        else:
            plan = plan_image(ctx, service, args, named=named)
        if plan is None:
            continue
        show(ctx, service, plan)
        if args.check:
            continue
        ctx.ui.confirm(f"Deploy {service.name}?", assume_yes=args.yes)
        if not deploy(ctx, service, plan, backup=not args.no_backup):
            return 1
    return 0
