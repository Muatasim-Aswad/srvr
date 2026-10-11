"""The shared local and CI verification gate."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, env: dict[str, str] | None = None) -> None:
    print("→ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def docker_checks() -> None:
    for image in ("alpine:latest", "python:3.14.6-slim-trixie", "registry:3"):
        present = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, check=False
        )
        if present.returncode:
            run("docker", "pull", image)
    run(
        sys.executable,
        "-m",
        "unittest",
        "tests.test_docker_workflow",
        "tests.test_deployment_acceptance",
        env={**os.environ, "SRVR_DOCKER_TESTS": "1", "SRVR_ACCEPTANCE_TESTS": "1"},
    )


def package_checks() -> None:
    run("scripts/build-deb")
    packages = list((ROOT / "dist").glob("srvr_*_all.deb"))
    package = max(packages, key=lambda path: path.stat().st_mtime_ns)
    run("scripts/test-deb", str(package))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--all", action="store_true", help="include Docker and packaging checks")
    modes.add_argument("--docker-only", action="store_true", help="run real Docker checks")
    modes.add_argument(
        "--package-only", action="store_true", help="build and test the Debian package"
    )
    args = parser.parse_args()
    if not (args.docker_only or args.package_only):
        run(sys.executable, "scripts/release.py", "check-source")
        run(sys.executable, "-m", "ruff", "check", ".")
        run(sys.executable, "-m", "ruff", "format", "--check", ".")
        run(sys.executable, "-m", "mypy", "srvr")
        run(sys.executable, "scripts/check_docs.py")
        workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        run("actionlint", *(str(path) for path in workflows))
        run(sys.executable, "-m", "unittest")
    if args.all or args.docker_only:
        docker_checks()
    if args.all or args.package_only:
        package_checks()


if __name__ == "__main__":
    main()
