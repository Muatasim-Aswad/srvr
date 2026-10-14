"""`srvr update`: prepare a candidate, switch with one backup, recover failed switches."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace

from srvr.commands._shared import add_yes, chosen_services
from srvr.core import backups
from srvr.core.context import Context
from srvr.core.deploymentplans import (
    Plan,
    capture_configuration,
    plan_configuration,
    plan_git,
    plan_image,
)
from srvr.core.deploymenttransaction import deploy, roll_back
from srvr.core.errors import SrvrError
from srvr.core.registry import Service

__all__ = ["Plan", "backups", "deploy", "plan_git", "plan_image", "roll_back"]

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
    parser.add_argument(
        "--set-image",
        action="append",
        default=[],
        metavar="COMPONENT=IMAGE",
        help="image services: select exact images for several components",
    )
    parser.add_argument(
        "--config", action="store_true", help="deploy changed registered configuration"
    )
    parser.add_argument(
        "--config-file",
        action="append",
        default=[],
        metavar="REGISTERED=CANDIDATE",
        help="stage replacement content for a registered configuration file",
    )
    parser.add_argument(
        "--capture-config",
        action="store_true",
        help="capture the current known-good configuration baseline before editing",
    )
    add_yes(parser)


def show(ctx: Context, service: Service, plan: Plan) -> None:
    ctx.ui.line(f"{ctx.ui.paint(service.name, 'bold')}: {plan.current} → {plan.target}")
    for commit in plan.commits:
        ctx.ui.line(f"  {commit}")
    if plan.warning:
        ctx.ui.warn(plan.warning)


def update_order(services: list[Service]) -> list[Service]:
    """Git services first, then image services (usually just listed as pinned), each group in
    its given order."""
    return sorted(services, key=lambda service: not service.is_git)


def validate_selection(args: Namespace, services: list[Service]) -> None:
    component = getattr(args, "component", None)
    selections = getattr(args, "set_image", [])
    config = getattr(args, "config", False) or bool(getattr(args, "config_file", []))
    capture = getattr(args, "capture_config", False)
    if (config or capture) and (args.ref or args.image or component or selections):
        raise SrvrError("configuration deployment cannot be combined with version selection")
    if capture and (config or args.check):
        raise SrvrError(
            "--capture-config cannot be combined with configuration deployment or --check"
        )
    if (args.ref or args.image or component or selections or config or capture) and len(
        services
    ) != 1:
        raise SrvrError(
            "version selection and configuration deployment require exactly one service"
        )
    if args.ref and any(not s.is_git for s in services):
        raise SrvrError("--ref is for git services; use --image for image services")
    if (args.image or selections) and any(s.is_git for s in services):
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
        if getattr(args, "capture_config", False):
            capture_configuration(ctx, service, args)
            continue
        plan: Plan | None
        if getattr(args, "config", False) or getattr(args, "config_file", []):
            plan = plan_configuration(ctx, service, args)
        elif service.is_git:
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
