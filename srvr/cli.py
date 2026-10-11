"""Entry point: discovers command modules and dispatches to them."""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
from collections.abc import Sequence
from types import ModuleType

from srvr import __version__, commands
from srvr.core.build import info as build_info
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.paths import Paths
from srvr.core.shell import Shell
from srvr.core.ui import UI

DEFAULT_COMMAND = "status"
_CONTRACT = ("HELP", "configure", "run")


def discover() -> dict[str, ModuleType]:
    """Command name -> module, for every public module in `srvr.commands`."""
    found = {}
    for info in pkgutil.iter_modules(commands.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{commands.__name__}.{info.name}")
        if missing := [attr for attr in _CONTRACT if not hasattr(module, attr)]:
            raise TypeError(f"command module {info.name} lacks {', '.join(missing)}")
        found[info.name.replace("_", "-")] = module
    return dict(sorted(found.items()))


def build_parser(modules: dict[str, ModuleType]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="srvr",
        description="Manage the Docker Compose services on this server.",
        epilog=f"Run `srvr` alone for `srvr {DEFAULT_COMMAND}`; `srvr <command> -h` for details.",
    )
    parser.add_argument("--version", action="version", version=f"srvr {__version__}")
    parser.add_argument("--build-info", action="store_true", help="show version and build identity")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every external command")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name, module in modules.items():
        command = sub.add_parser(name, help=module.HELP, description=module.HELP)
        command.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help="print every external command",
        )
        module.configure(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    modules = discover()
    parser = build_parser(modules)
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    if args.build_info:
        print(json.dumps(build_info(Shell()), indent=2))
        return 0
    if args.command is None:
        args = parser.parse_args([*argv, DEFAULT_COMMAND])

    ui = UI()
    ctx = Context(paths=Paths.from_env(), shell=Shell(verbose=args.verbose), ui=ui)
    try:
        status: int = modules[args.command].run(args, ctx)
        return status
    except SrvrError as exc:
        ui.error(str(exc))
        return 1
    except OSError as exc:  # unreadable file, missing directory, ...
        ui.error(f"{exc.strerror or exc}: {exc.filename or ''}".rstrip(": "))
        return 1
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130
