"""Terminal output: status lines, tables, confirmations, human-readable values."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TextIO

from .errors import SrvrError

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_COLORS = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2", "bold": "1"}
_STATE_COLORS = {
    "healthy": "green",
    "running": "green",
    "ok": "green",
    "starting": "yellow",
    "stopped": "yellow",
    "warn": "yellow",
    "absent": "dim",
    "unhealthy": "red",
    "restarting": "red",
    "failed": "red",
    "rolled-back": "yellow",
    "fail": "red",
}


class UI:
    def __init__(self, *, out: TextIO | None = None, err: TextIO | None = None) -> None:
        # resolved at call time so redirected streams are honoured
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.color = self.out.isatty() and "NO_COLOR" not in os.environ

    def paint(self, text: str, color: str) -> str:
        return f"\x1b[{_COLORS[color]}m{text}\x1b[0m" if self.color else text

    def state(self, text: str) -> str:
        """Color a state word (healthy, stopped, ...) by meaning."""
        key = text.split(maxsplit=1)[0] if text else ""
        color = _STATE_COLORS.get(key) or ("yellow" if "/" in key else None)
        return self.paint(text, color) if color else text

    def line(self, text: str = "") -> None:
        print(text, file=self.out)

    def step(self, text: str) -> None:
        print(self.paint(f"→ {text}", "cyan"), file=self.out)

    def ok(self, text: str) -> None:
        print(self.paint(f"✓ {text}", "green"), file=self.out)

    def warn(self, text: str) -> None:
        print(self.paint(f"! {text}", "yellow"), file=self.err)

    def error(self, text: str) -> None:
        print(self.paint(f"✗ {text}", "red"), file=self.err)

    def section(self, title: str) -> None:
        """Start a separated block, e.g. one service in a multi-service run."""
        width = min(shutil.get_terminal_size((80, 24)).columns, 80)
        rule = "─" * max(3, width - len(title) - 4)
        print(file=self.out)
        print(self.paint(f"── {title} {rule}", "bold"), file=self.out)

    def fields(self, pairs: Sequence[tuple[str, str]]) -> None:
        width = max((len(key) for key, _ in pairs), default=0)
        for key, value in pairs:
            self.line(f"{self.paint(key.ljust(width), 'dim')}  {value}")

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        self.line(render_table(headers, rows, header_style=lambda h: self.paint(h, "bold")))

    def confirm(self, question: str, *, assume_yes: bool) -> None:
        """Raise unless the user agrees (or --yes was given)."""
        if assume_yes:
            return
        if not sys.stdin.isatty():
            raise SrvrError("confirmation required; rerun with --yes")
        answer = input(f"{question} [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise SrvrError("cancelled")


def visible_len(text: str) -> int:
    return len(_ANSI.sub("", text))


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    header_style: Callable[[str], str] = str,
) -> str:
    widths = [visible_len(h) for h in headers]
    for row in rows:
        widths = [max(w, visible_len(cell)) for w, cell in zip(widths, row, strict=True)]

    def fmt(cells: Sequence[str]) -> str:
        padded = [
            cell + " " * (w - visible_len(cell)) for cell, w in zip(cells, widths, strict=True)
        ]
        return "  ".join(padded).rstrip()

    lines = [fmt([header_style(h.ljust(w)) for h, w in zip(headers, widths, strict=True)])]
    lines += [fmt(row) for row in rows]
    return "\n".join(lines)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def ago(moment: datetime, now: datetime | None = None) -> str:
    seconds = int(((now or utc_now()) - moment).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
