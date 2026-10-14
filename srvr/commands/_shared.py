"""Argument and presentation helpers shared by several commands."""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Iterator
from contextlib import contextmanager

from srvr.core import deploymentprep, history
from srvr.core.context import Context
from srvr.core.deploys import wait_ready
from srvr.core.docker import summarize
from srvr.core.errors import SrvrError
from srvr.core.registry import Service
from srvr.core.ui import ago, parse_iso


def add_services(parser: ArgumentParser, *, require_choice: bool = False) -> None:
    """Positional service names. With `require_choice`, an empty list needs --all."""
    parser.add_argument("services", nargs="*", metavar="SERVICE", help="service name(s)")
    if require_choice:
        parser.add_argument("--all", action="store_true", help="every registered service")


def chosen_services(ctx: Context, args: Namespace) -> list[Service]:
    """Services from `add_services(require_choice=True)`; refuses an implicit 'all'."""
    if args.services and getattr(args, "all", False):
        raise SrvrError("give service names or --all, not both")
    if not args.services and not getattr(args, "all", False):
        raise SrvrError("name at least one service, or pass --all")
    return ctx.services(args.services)


def add_yes(parser: ArgumentParser) -> None:
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")


def docker_access(ctx: Context, *, prompt: bool) -> bool:
    """Whether Docker (via sudo) can be used, prompting for the password only if allowed."""
    if ctx.shell.has_sudo():
        return True
    if not prompt or not sys.stdin.isatty():
        return False
    try:
        ctx.shell.ensure_sudo()
    except SrvrError:
        return False
    return True


@contextmanager
def stopped(ctx: Context, service: Service) -> Iterator[bool]:
    """Stop the service if it runs and start it again afterwards; yields whether it was running."""
    compose = ctx.compose(service)
    exclude = [service.migrate] if service.migrate else []
    containers = ctx.docker.project_containers(service.project, exclude=exclude, refresh=True)
    was_running = summarize(containers) not in ("stopped", "absent")
    if was_running:
        ctx.ui.step(f"{service.name}: stopping")
        compose.stop()
    try:
        yield was_running
    finally:
        if was_running:
            ctx.ui.step(f"{service.name}: starting")
            compose.start()


def short_image(ref: str) -> str:
    """`docker.io/library/memos:0.30.0@sha256:...` -> `memos:0.30.0`."""
    return ref.split("@", 1)[0].rsplit("/", 1)[-1]


def version_of(ctx: Context, service: Service) -> str:
    """What is deployed: `<commit> (<branch>|detached)` or the image tag(s)."""
    if service.is_git:
        checkout = ctx.checkout(service)
        return f"{checkout.short('HEAD')} ({checkout.branch() or 'detached'})"
    refs = ctx.compose(service).image_refs()
    shown = deploymentprep.display_refs(ctx, service, refs)
    return ", ".join(sorted({short_image(ref) for ref in shown.values()})) or "-"


def last_event(ctx: Context, service: Service) -> str:
    """The last change, skipping the nightly snapshots that would otherwise always win."""
    events = [e for e in history.read(ctx.paths, service.name) if e.action != "snapshot"]
    if not events:
        return "-"
    event = events[0]
    return f"{event.action} {event.result}, {ago(parse_iso(event.time))}"


def report_ready(ctx: Context, service: Service) -> bool:
    """Wait for readiness and print the outcome."""
    result = wait_ready(ctx, service)
    if result.ok:
        ctx.ui.ok(f"{service.name}: ready ({result.detail})")
    else:
        ctx.ui.error(f"{service.name}: not ready ({result.detail}); see `srvr logs {service.name}`")
    return result.ok
