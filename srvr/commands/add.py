"""`srvr add`: guided registration of an existing Compose project."""

from __future__ import annotations

import shlex
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from srvr.commands import backup, health
from srvr.commands._shared import add_yes, report_ready
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.onboarding import (
    save_registration,
    validate_files,
    validate_model,
    validate_unique,
    writable_binds,
)
from srvr.core.registry import NAME_RE, Service, parse_service

HELP = "Register an existing Compose project, validate it, and optionally start and back it up"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name (lowercase letters, digits and hyphens)")
    parser.add_argument("--workdir", help="existing project directory")
    parser.add_argument("--kind", choices=("git", "image"), help="source build or published images")
    parser.add_argument("--branch", help="Git deployment branch")
    parser.add_argument(
        "--compose-file", action="append", help="Compose file; repeat for overrides"
    )
    parser.add_argument("--env-file", help="existing Compose interpolation env file")
    parser.add_argument(
        "--config-file", action="append", help="existing config file; repeat as needed"
    )
    parser.add_argument("--data", action="append", help="existing data directory; repeat as needed")
    parser.add_argument("--migrate", help="one-off migration component")
    parser.add_argument("--health", help="loopback health URL")
    parser.add_argument("--url", help="private HTTPS URL, including any shared-service path")
    parser.add_argument("--version-url", help="Git app's loopback version.json URL")
    parser.add_argument("--update-component", help="default component for published-image updates")
    parser.add_argument("--project", help="Compose project name (defaults to service name)")
    parser.add_argument("--description", help="short service description")
    parser.add_argument(
        "--start", action="store_true", help="prepare images, start and check health"
    )
    parser.add_argument("--backup", action="store_true", help="take the first local data backup")
    add_yes(parser)


def ask(label: str, default: str = "") -> str:
    try:
        answer = input(f"{label}" + (f" [{default}]" if default else " (optional)") + ": ").strip()
    except EOFError as exc:
        raise SrvrError("onboarding interrupted; no registration saved") from exc
    return default if not answer else ("" if answer == "-" else answer)


def validate_source(ctx: Context, service: Service) -> None:
    if service.is_git:
        checkout = ctx.checkout(service)
        if not checkout.is_clean():
            raise SrvrError("source checkout has local changes; resolve them before registration")
        assert service.branch
        checkout.resolve(service.branch)


def collect(args: Namespace, ctx: Context) -> tuple[Service, dict[str, Any], list[str]]:
    interactive = sys.stdin.isatty() and not args.yes
    if interactive:
        ctx.ui.line("Register existing files; use '-' to leave an optional value empty.")

    def value(key: str, label: str, default: str = "") -> str:
        supplied = getattr(args, key)
        if supplied is not None:
            return str(supplied)
        return ask(label, default) if interactive else default

    def paths(key: str, label: str, default: list[str] | None = None) -> list[str]:
        supplied = getattr(args, key)
        if supplied is not None:
            return list(supplied)
        if not interactive:
            return default or []
        try:
            return shlex.split(
                ask(
                    label + " (space-separated; quote paths with spaces)", shlex.join(default or [])
                )
            )
        except ValueError as exc:
            raise SrvrError("invalid quoting in path list") from exc

    workdir_value = value(
        "workdir",
        "Project directory",
        str(Path.home() / "services" / args.service) if interactive else "",
    )
    if not workdir_value:
        raise SrvrError("give --workdir, or run add interactively")
    workdir = Path(workdir_value).expanduser().resolve()
    kind = value("kind", "Kind: git or image", "git" if (workdir / ".git").exists() else "image")
    raw: dict[str, Any] = {"kind": kind, "workdir": str(workdir)}

    def absolute(item: str) -> str:
        path = Path(item).expanduser()
        return str((path if path.is_absolute() else workdir / path).resolve())

    default_compose = next(
        (
            name
            for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
            if (workdir / name).is_file()
        ),
        "compose.yaml",
    )
    raw["compose_files"] = [
        absolute(p) for p in paths("compose_file", "Compose files in order", [default_compose])
    ]
    for key, label in (
        ("project", "Compose project name"),
        ("description", "Description"),
        ("env_file", "Interpolation env file"),
    ):
        answer = value(key, label, args.service if key == "project" else "")
        if answer:
            raw[key] = absolute(answer) if key == "env_file" else answer
    config = paths("config_file", "Config files", [raw["env_file"]] if "env_file" in raw else [])
    if config or args.config_file is not None:
        raw["config_files"] = [absolute(p) for p in config]
    if kind == "git":
        raw["branch"] = args.branch or "main"
        provisional = parse_service(
            args.service, raw, ctx.paths.services_dir / f"{args.service}.toml"
        )
        branch = ctx.checkout(provisional).branch() if interactive and workdir.is_dir() else None
        raw["branch"] = value("branch", "Deployment branch", branch or "main")
    elif args.branch is not None:
        raw["branch"] = args.branch
    service = parse_service(args.service, raw, ctx.paths.services_dir / f"{args.service}.toml")
    validate_files(service)
    model = ctx.compose(service).config(all_profiles=True)
    raw["data"] = [
        absolute(p)
        for p in paths(
            "data",
            "Data directories to back up",
            [str(p) for p in writable_binds(model) if p.is_dir()] if interactive else [],
        )
    ]
    for key, label in (
        ("migrate", "Migration component"),
        ("health", "Loopback health URL"),
        ("url", "Private HTTPS URL"),
        ("version_url", "Loopback version.json URL"),
        ("update_component", "Default image component to update"),
    ):
        if (
            (key == "version_url" and kind != "git")
            or (key == "update_component" and kind != "image")
        ) and getattr(args, key) is None:
            continue
        answer = value(key, label)
        if answer:
            raw[key] = answer
    service = parse_service(args.service, raw, ctx.paths.services_dir / f"{args.service}.toml")
    validate_files(service)
    validate_source(ctx, service)
    return service, raw, validate_model(service, model)


def next_steps(ctx: Context, service: Service) -> None:
    ctx.ui.line(f"Inspect: srvr info {service.name}")
    ctx.ui.line(f"Validate files: srvr compose {service.name} config --quiet")
    action = "update" if service.is_git else "start"
    extra = " --force" if service.is_git else ""
    ctx.ui.line(f"Deploy: srvr {action} {service.name}{extra}; verify: srvr health {service.name}")
    if service.is_git:
        ctx.ui.line(
            "Private repository: use a per-repository read-only deploy key; "
            "register its public half from your administration computer."
        )
    if service.url:
        ctx.ui.line(
            "Private HTTPS: define the Tailscale Service and manually approve its host "
            "in the admin console; configure only the intended Serve path "
            "and preserve existing handlers."
        )
        ctx.ui.line("Verify: tailscale serve status; then srvr health " + service.name)
    if service.data:
        ctx.ui.line(f"Local backup: srvr backup {service.name}")
        ctx.ui.line(
            "Registered data joins `srvr snapshot --all` when an off-server backup job "
            "is configured. Registration does not install or verify that job."
        )
    else:
        ctx.ui.line("No data directories registered; srvr will not back up this service's data.")
    ctx.ui.line("Offline index: srvr info --all --markdown")


def run(args: Namespace, ctx: Context) -> int:
    if not NAME_RE.fullmatch(args.service):
        raise SrvrError(f"service name must match {NAME_RE.pattern}")
    target = ctx.paths.services_dir / f"{args.service}.toml"
    if target.exists() or target.is_symlink():
        raise SrvrError(f"{args.service}: already registered; use srvr info")
    service, raw, warnings = collect(args, ctx)
    if args.backup and not service.data:
        raise SrvrError("--backup needs at least one --data directory")
    ctx.ui.fields([(key, str(item)) for key, item in raw.items()])
    for directory in service.data:
        stat = directory.stat()
        ctx.ui.line(
            f"Data owner {directory}: {stat.st_uid}:{stat.st_gid}, mode {stat.st_mode & 0o777:04o}"
        )
    for warning in warnings:
        ctx.ui.warn(warning)
    ctx.ui.confirm(f"Register {service.name}?", assume_yes=args.yes)
    with service_lock(ctx.paths, "_registry"), service_lock(ctx.paths, service.name):
        validate_unique(ctx.paths, service)
        file = save_registration(ctx.paths, service, raw)
    ctx.ui.ok(f"registered {service.name}: {file}")
    next_steps(ctx, service)
    if args.start:
        ctx.shell.ensure_sudo()
        with service_lock(ctx.paths, service.name):
            if any(
                container.state == "running"
                for container in ctx.docker.project_containers(service.project, refresh=True)
            ):
                raise SrvrError(
                    f"{service.name}: project is already running; registration saved, "
                    "use srvr update for deployment"
                )
            compose = ctx.compose(service)
            if service.is_git:
                compose.build(ctx.checkout(service).build_stamp())
            compose.pull()
            if service.migrate:
                compose.run_once(service.migrate)
            compose.up(pull=False)
            if not report_ready(ctx, service):
                return 1
        if health.run(Namespace(services=[service.name]), ctx) != 0:
            return 1
    if args.backup:
        ctx.shell.ensure_sudo()
        backup.backup_one(ctx, service)
    return 0
