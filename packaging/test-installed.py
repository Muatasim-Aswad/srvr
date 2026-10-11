"""Exercise the installed package in a disposable Debian container, as an ordinary user."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "href" and value:
                self.links.append(value)
            if key == "id" and value:
                self.ids.add(value)


def check_docs() -> None:
    root = Path("/usr/share/doc/srvr")
    pages = {}
    for path in root.rglob("*.html"):
        page = Links()
        page.feed(path.read_text())
        pages[path.resolve()] = page
    assert root / "index.html" in pages
    assert len(pages) >= 8, "missing offline guides"
    for path, page in pages.items():
        for link in page.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            assert target.is_relative_to(root), (path, link)
            assert target.exists(), (path, link)
            if url.fragment and target in pages:
                assert unquote(url.fragment) in pages[target].ids, (path, link)
    for name in ("bootstrap", "backups", "monitoring", "commands", "services", "updates"):
        text = gzip.decompress((root / "docs" / f"{name}.md.gz").read_bytes()).decode()
        assert text.startswith("# "), name
    assert "Permission is hereby granted" in (root / "copyright").read_text()


def check_cli() -> None:
    installed = subprocess.run(
        ["dpkg-query", "--showformat=${Version}", "--show", "srvr"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("-", 1)[0]
    result = subprocess.run(
        ["runuser", "-u", "nobody", "--", "srvr", "--version"],
        cwd="/tmp",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"srvr {installed}"
    result = subprocess.run(
        ["runuser", "-u", "nobody", "--", "srvr", "--build-info"],
        cwd="/tmp",
        capture_output=True,
        text=True,
        check=True,
    )
    identity = json.loads(result.stdout)
    assert identity["version"] == installed
    assert identity["revision"]
    assert identity["commit_time"] and identity["build_time"]
    commands = [
        "add",
        "backup",
        "backups",
        "compose",
        "config",
        "doctor",
        "health",
        "history",
        "info",
        "logs",
        "prune",
        "restart",
        "restore",
        "rollback",
        "snapshot",
        "start",
        "status",
        "stop",
        "update",
    ]
    for command in commands:
        result = subprocess.run(
            ["runuser", "-u", "nobody", "--", "srvr", command, "-h"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=True,
        )
        assert f"srvr {command}" in result.stdout
    with tempfile.TemporaryDirectory(prefix="srvr-package-user-") as directory:
        root = Path(directory)
        config = root / "config/srvr/services"
        config.mkdir(parents=True)
        settings = root / "settings.env"
        settings.write_text("PORT=8080\nAPI_TOKEN=package-test-secret\n")
        (config / "notes.toml").write_text(
            f'kind = "image"\nworkdir = "{root}"\nconfig_files = ["{settings}"]\n'
        )
        for path in [root, *root.rglob("*")]:
            os.chown(path, 65534, 65534)
        result = subprocess.run(
            [
                "runuser",
                "-u",
                "nobody",
                "--",
                "env",
                f"HOME={root}",
                f"XDG_CONFIG_HOME={root / 'config'}",
                "srvr",
                "config",
                "notes",
            ],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=True,
        )
        assert "PORT" in result.stdout and "8080" in result.stdout
        assert "API_TOKEN" in result.stdout and "package-test-secret" not in result.stdout


if __name__ == "__main__":
    check_cli()
    check_docs()
    manual = subprocess.run(
        ["man", "--pager=cat", "srvr"], capture_output=True, text=True, check=True
    ).stdout
    assert "SYNOPSIS" in manual and "srvr update" in manual and "srvr rollback" in manual
    print("Installed CLI, redacted config, offline links/anchors, and man page passed.")
