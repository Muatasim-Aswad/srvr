"""`srvr info`: everything about a service in one place, or a Markdown index of all of them."""

from __future__ import annotations

import shlex
from argparse import ArgumentParser, Namespace

from srvr.commands._shared import last_event, version_of
from srvr.core import backups, deploymentprep, deploys
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.registry import Service
from srvr.core.ui import ago, human_size, parse_iso

HELP = "Paths, URLs, version, backups and rollback state; --all --markdown for an offline index"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", nargs="?", help="service name")
    parser.add_argument("--all", action="store_true", help="every registered service")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print a Markdown index and emergency Compose commands (for offline records)",
    )


def details(ctx: Context, s: Service) -> list[tuple[str, str]]:
    readme = s.workdir / "README.md"
    saved = backups.list_backups(ctx.paths, s.name)
    record = deploys.load_record(ctx.paths, s.name)
    pairs = [
        ("service", s.name + (f" — {s.description}" if s.description else "")),
        ("kind", s.kind + (f", branch {s.branch}" if s.branch else "")),
        ("version", version_of(ctx, s)),
        ("url", s.url or "-"),
        ("health", s.health or "- (container state only)"),
        ("project", s.project),
        ("workdir", str(s.workdir)),
        ("compose", ", ".join(str(f) for f in s.compose_files)),
        ("env file", str(s.env_file) if s.env_file else "-"),
        ("migrate", s.migrate or "-"),
        ("update component", s.update_component or "-"),
        ("profiles", ", ".join(s.profiles) or "- (unprofiled components only)"),
        ("public ports", "allowed by registration" if s.allow_public_ports else "loopback only"),
        ("host network", "allowed by registration" if s.allow_host_network else "refused"),
        ("data directories", ", ".join(str(d) for d in s.data) or "-"),
        ("data volumes", ", ".join(s.data_volumes) or "-"),
        (
            "backup coverage",
            "registered data only; other mounts unmanaged" if s.has_data else "none",
        ),
        ("readme", str(readme) if readme.exists() else "-"),
        ("registry", str(s.source)),
    ]
    if saved:
        newest = saved[0]
        when = ago(parse_iso(newest.meta["time"])) if "time" in newest.meta else newest.name
        pairs.append(
            ("backups", f"{len(saved)}; newest {newest.name} ({human_size(newest.size)}, {when})")
        )
    else:
        pairs.append(("backups", "none"))
    pairs.append(("last change", last_event(ctx, s)))
    if record and record.status == "ok":
        pairs.append(("rollback", f"available to {record.before.version()}"))
    return pairs


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def markdown(ctx: Context, services: list[Service]) -> str:
    """Offline index plus the raw Compose command for each service, for when srvr is unusable."""
    lines = [
        "| Service | Kind | Private URL | Loopback health | Source | Data |",
        "|---|---|---|---|---|---|",
    ]
    for s in services:
        if s.is_git:
            source = f"Git `{s.branch}` in `{s.workdir}`"
        else:
            refs = sorted(
                set(deploymentprep.display_refs(ctx, s, ctx.compose(s).image_refs()).values())
            )
            source = ", ".join(f"`{ref}`" for ref in refs) or "-"
        cells = [
            s.name,
            s.kind,
            f"`{s.url}`" if s.url else "-",
            f"`{s.health}`" if s.health else "-",
            source,
            ", ".join([*(f"`{d}`" for d in s.data), *(f"volume `{v}`" for v in s.data_volumes)])
            or "none",
        ]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    lines += ["", "Emergency Compose commands, for when srvr itself cannot run:", "", "```sh"]
    for s in services:
        lines.append(
            f"# {s.name}: profiles {', '.join(s.profiles) or '(none)'}; "
            f"public ports {'allowed' if s.allow_public_ports else 'refused'}; "
            f"host networking {'allowed' if s.allow_host_network else 'refused'}"
        )
        command = shlex.join(ctx.compose(s).argv())
        lines.append(f"cd {shlex.quote(str(s.workdir))} && sudo {command} <args>  # {s.name}")
    lines.append("```")
    return "\n".join(lines)


def run(args: Namespace, ctx: Context) -> int:
    if args.all and args.service:
        raise SrvrError("give a service name or --all, not both")
    if not args.all and not args.service:
        raise SrvrError("name a service, or pass --all")
    services = ctx.services([]) if args.all else [ctx.service(args.service)]

    if args.markdown:
        ctx.ui.line(markdown(ctx, services))
        return 0
    for service in services:
        if len(services) > 1:
            ctx.ui.section(service.name)
        ctx.ui.fields(details(ctx, service))
    return 0
