"""Check local Markdown navigation, example syntax, and shell entrypoints without executing them."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def anchors(path: Path) -> set[str]:
    return {
        re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^#+ (.+)$", path.read_text(), re.MULTILINE)
    }


def main() -> None:
    files = [
        *(ROOT / name for name in ("README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md")),
        *sorted((ROOT / "docs").glob("*.md")),
    ]
    count = 0
    for path in files:
        content = path.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                continue
            name, _, anchor = target.partition("#")
            dest = path.parent / name if name else path
            if not dest.exists() or (anchor and anchor not in anchors(dest)):
                raise ValueError(f"{path.relative_to(ROOT)}: broken link {target}")
            count += 1
        for language, code in re.findall(
            r"^```(\w+)\n(.*?)^```", content, re.MULTILINE | re.DOTALL
        ):
            if language == "sh":
                subprocess.run(["sh", "-n"], input=code, text=True, check=True)
            elif language == "toml":
                tomllib.loads(code)
    for path in [*sorted((ROOT / "scripts").iterdir()), *sorted((ROOT / ".githooks").iterdir())]:
        if path.is_file() and path.read_bytes().startswith(b"#!"):
            shell = "bash" if b"bash" in path.read_bytes().splitlines()[0] else "sh"
            subprocess.run([shell, "-n", str(path)], check=True)
    print(f"{count} Markdown links checked; example and shell syntax valid.")


if __name__ == "__main__":
    main()
