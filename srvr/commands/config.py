"""`srvr config`: inspect or edit a service's settings files without exposing secrets."""

from __future__ import annotations

import os
import shlex
import shutil
from argparse import ArgumentParser, Namespace

from srvr.core import envfile
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock

HELP = "Show a service's settings (secret values hidden), or edit them"


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("service", help="service name")
    parser.add_argument("--edit", action="store_true", help="open the settings in $VISUAL/$EDITOR")


def _editor() -> list[str]:
    for candidate in (os.environ.get("VISUAL"), os.environ.get("EDITOR"), "nano", "vi"):
        if candidate and shutil.which(shlex.split(candidate)[0]):
            return shlex.split(candidate)
    raise SrvrError("no editor found; set $EDITOR")


def run(args: Namespace, ctx: Context) -> int:
    service = ctx.service(args.service)
    files = service.config_files
    if args.edit:
        if not files:
            raise SrvrError(f"{service.name}: no config_files registered")
        with service_lock(ctx.paths, service.name):
            ctx.shell.run([*_editor(), *map(str, files)], capture=False)
        ctx.ui.line(f"apply changes with: srvr start {service.name}")
        return 0

    ctx.ui.fields(
        [
            ("registry", str(service.source)),
            ("compose", ", ".join(str(f) for f in service.compose_files)),
        ]
    )
    for file in files:
        ctx.ui.line()
        ctx.ui.line(ctx.ui.paint(str(file), "bold"))
        if not file.exists():
            ctx.ui.warn("missing")
            continue
        pairs = envfile.parse(file.read_text())
        ctx.ui.fields([(key, envfile.display_value(key, value)) for key, value in pairs])
    return 0
