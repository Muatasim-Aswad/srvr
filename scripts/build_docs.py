"""Build offline guides and the man page from the repository's Markdown."""

from __future__ import annotations

import argparse
import gzip
import os
import re
import subprocess
import tomllib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    changelog = (ROOT / "debian/changelog").read_text()
    if not changelog.startswith(f"srvr ({project}-"):
        raise ValueError("debian/changelog and project version disagree")
    return str(project)


def release_date() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%d")
    stamp = (ROOT / "debian/changelog").read_text().split("  ")[-1].strip()
    return parsedate_to_datetime(stamp).strftime("%Y-%m-%d")


def manual(markdown: str) -> str:
    body = markdown.split("\n", 1)[1]
    body = re.sub(r"^\[README\].*\n", "", body, flags=re.MULTILINE)
    table = re.search(r"^\| Command \| Purpose \|\n(?:\|.*\n)+", body, re.MULTILINE)
    if table is None:
        raise ValueError("command reference has no command table")
    entries = []
    for row in table.group().splitlines()[2:]:
        command, purpose = re.split(r"(?<!\\)\|", row)[1:3]
        command = command.strip().replace("\\|", "|")
        entries.append(f"{command}\n:   {purpose.strip()}\n")
    body = body[: table.start()] + "\n".join(entries) + body[table.end() :]
    body = re.sub(r"^## ", "# ", body, flags=re.MULTILINE)
    return (
        "# NAME\n\nsrvr - manage a private Docker Compose home server\n\n"
        "# SYNOPSIS\n\n`srvr` [OPTION] COMMAND [ARGUMENT ...]\n\n"
        "`srvr` (without a command, shows status)\n\n# DESCRIPTION\n\n" + body + "\n# SEE ALSO\n\n"
        "`docker`(1), `git`(1). Offline guides: `/usr/share/doc/srvr/index.html`; "
        "Markdown copies: `/usr/share/doc/srvr/docs/*.md.gz`.\n"
    )


def build(output: Path) -> None:
    package_version = version()
    docs = output / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for source in [
        *(ROOT / name for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md")),
        *sorted((ROOT / "docs").glob("*.md")),
    ]:
        relative = source.relative_to(ROOT)
        target = docs / relative.with_suffix(".html")
        if source.name == "README.md":
            target = docs / "index.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text()
        title = content.splitlines()[0].removeprefix("# ")
        subprocess.run(
            [
                "pandoc",
                str(source),
                "--from=gfm",
                "--to=html5",
                "--standalone",
                "--lua-filter",
                str(ROOT / "scripts/docs-links.lua"),
                "--metadata",
                f"pagetitle={title}",
                "--metadata",
                "lang=en",
                "--output",
                str(target),
            ],
            check=True,
        )
        compressed = docs / relative.with_suffix(".md.gz")
        compressed.write_bytes(gzip.compress(content.encode(), mtime=0))
    subprocess.run(
        [
            "pandoc",
            "--from=markdown",
            "--to=man",
            "--standalone",
            "--metadata",
            "title=SRVR",
            "--metadata",
            "section=1",
            "--metadata",
            f"footer=srvr {package_version}",
            "--metadata",
            f"date={release_date()}",
            "--output",
            str(output / "srvr.1"),
        ],
        input=manual((ROOT / "docs/commands.md").read_text()),
        text=True,
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".build")
    build(parser.parse_args().output)
