"""Build and inspect a release from an explicit source inventory; never publish locally."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def version(root: Path) -> str:
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])


def inventory(root: Path) -> list[str]:
    names = (root / "packaging/source-files.txt").read_text().splitlines()
    if names != sorted(set(names)) or not names:
        raise ValueError("source inventory must be sorted and unique")
    for name in names:
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(part in ("local", "plans", "dist", ".git", "__pycache__") for part in path.parts)
            or name.endswith((".local.md", ".db", ".sqlite", ".sqlite3", ".pyc", ".map"))
            or any(part.startswith(".env") for part in path.parts)
            or not (root / name).is_file()
            or (root / name).is_symlink()
        ):
            raise ValueError(f"unsafe or missing source file: {name}")
    if (root / ".git").exists():
        tracked = git("ls-files", root=root).splitlines()
        if sorted(tracked) != names:
            raise ValueError("tracked files differ from packaging/source-files.txt")
    return names


def identity(root: Path) -> dict[str, str]:
    stamp = root / "srvr/build.json"
    if stamp.exists() and not (root / ".git").exists():
        return dict(json.loads(stamp.read_text()))
    commit_time = parsedate_to_datetime(
        (root / "debian/changelog").read_text().split("  ")[-1].strip()
    )
    revision = "dev"
    if (root / ".git").exists():
        revision = git("rev-parse", "HEAD", root=root)
        commit_time = datetime.fromtimestamp(int(git("show", "-s", "--format=%ct", root=root)), UTC)
        if git("status", "--porcelain", "--untracked-files=no", root=root):
            revision = "dev"
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", str(int(commit_time.timestamp()))))
    return {
        "version": version(root),
        "revision": revision,
        "commit_time": commit_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "build_time": datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z"),
    }


def stage(root: Path, output: Path, record: dict[str, str] | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("staging directory must be empty")
    for name in inventory(root):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
        target.chmod(0o755 if (root / name).stat().st_mode & 0o111 else 0o644)
    (output / "srvr/build.json").write_text(json.dumps(record or identity(root), indent=2) + "\n")


def scan(root: Path) -> None:
    run("gitleaks", "dir", str(root), "--no-banner", "--redact", cwd=root)


def check_source(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="srvr-source-check-") as directory:
        snapshot = Path(directory)
        stage(root, snapshot)
        scan(snapshot)
    print("Source inventory and secret scan passed.")


def scan_range(before: str, after: str) -> None:
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (before, after)):
        raise ValueError("scan boundaries must be commit SHAs")
    commits = after if before == "0" * 40 else f"{before}..{after}"
    run("gitleaks", "git", str(ROOT), "--no-banner", "--redact", f"--log-opts={commits}")


def source_archive(root: Path, output: Path, epoch: int) -> None:
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name in inventory(root):
            data = (root / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mtime = epoch
            member.mode = 0o755 if (root / name).stat().st_mode & 0o111 else 0o644
            archive.addfile(member, io.BytesIO(data))
    output.write_bytes(gzip.compress(content.getvalue(), mtime=0))


def deb_archives(path: Path) -> dict[str, bytes]:
    blob = path.read_bytes()
    if not blob.startswith(b"!<arch>\n"):
        raise ValueError("not a Debian archive")
    archives = {}
    offset = 8
    while offset < len(blob):
        header = blob[offset : offset + 60]
        if len(header) != 60 or header[58:60] != b"`\n":
            raise ValueError("invalid Debian member")
        size = int(header[48:58])
        name = header[:16].decode().strip().rstrip("/")
        if name in archives or len(blob[offset + 60 : offset + 60 + size]) != size:
            raise ValueError("duplicate or truncated Debian member")
        archives[name] = blob[offset + 60 : offset + 60 + size]
        offset += 60 + size + size % 2
    if set(archives) != {"debian-binary", "control.tar.xz", "data.tar.xz"}:
        raise ValueError("unexpected Debian archive contents or compression")
    return archives


def package_files(root: Path) -> set[str]:
    expected = {
        "usr/bin/srvr",
        "usr/lib/srvr/bin/srvr",
        "usr/lib/srvr/pyproject.toml",
        "usr/lib/srvr/srvr/build.json",
        "usr/share/doc/srvr/LICENSE",
        "usr/share/doc/srvr/copyright",
        "usr/share/doc/srvr/changelog.Debian.gz",
        "usr/share/doc/srvr/examples/service.toml",
        "usr/share/man/man1/srvr.1.gz",
        "usr/share/lintian/overrides/srvr",
        "usr/share/python3/runtime.d/srvr.rtupdate",
    }
    for name in inventory(root):
        if name.startswith("srvr/"):
            expected.add("usr/lib/srvr/" + name)
        if name in ("README.md", "CONTRIBUTING.md", "SECURITY.md") or (
            name.startswith("docs/") and name.endswith(".md")
        ):
            expected.add("usr/share/doc/srvr/" + name + ".gz")
            html = "index.html" if name == "README.md" else name.removesuffix(".md") + ".html"
            expected.add("usr/share/doc/srvr/" + html)
    return expected


def check_packaged_source(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo], root: Path
) -> None:
    direct = {"usr/lib/srvr/" + name: name for name in inventory(root) if name.startswith("srvr/")}
    direct.update(
        {
            "usr/lib/srvr/pyproject.toml": "pyproject.toml",
            "usr/lib/srvr/bin/srvr": "bin/srvr",
            "usr/share/doc/srvr/LICENSE": "LICENSE",
            "usr/share/doc/srvr/copyright": "debian/copyright",
            "usr/share/doc/srvr/examples/service.toml": "examples/service.toml",
        }
    )
    for installed, source in direct.items():
        content = archive.extractfile(members[installed])
        expected = (root / source).read_bytes()
        if source == "bin/srvr":
            expected = expected.replace(b"#!/usr/bin/python3\n", b"#! /usr/bin/python3\n", 1)
        if content is None or content.read() != expected:
            raise ValueError(f"packaged source differs: {installed}")
    for installed, member in members.items():
        if installed.endswith(".md.gz"):
            source = installed.removeprefix("usr/share/doc/srvr/").removesuffix(".gz")
            content = archive.extractfile(member)
            if content is None or gzip.decompress(content.read()) != (root / source).read_bytes():
                raise ValueError(f"packaged documentation differs: {installed}")


def check_package(path: Path, root: Path = ROOT) -> dict[str, str]:
    archives = deb_archives(path)
    with tarfile.open(fileobj=io.BytesIO(archives["control.tar.xz"])) as control:
        entries = [member for member in control if not member.isdir()]
        names = {member.name.removeprefix("./") for member in entries}
        if len(names) != len(entries) or names != {"control", "md5sums", "postinst", "prerm"}:
            raise ValueError(f"unexpected package control files: {sorted(names)}")
        if any(not member.isfile() for member in entries):
            raise ValueError("package controls must be regular files")
    with tarfile.open(fileobj=io.BytesIO(archives["data.tar.xz"])) as archive:
        directories = {
            member.name.removeprefix("./").rstrip("/") for member in archive if member.isdir()
        }
        expected_directories = {"."}
        for name in package_files(root):
            expected_directories.update(str(parent) for parent in Path(name).parents)
        if directories != expected_directories:
            raise ValueError("package directory inventory differs")
        entries = [member for member in archive if not member.isdir()]
        members = {member.name.removeprefix("./"): member for member in entries}
        if len(members) != len(entries) or set(members) != package_files(root):
            raise ValueError(
                f"package inventory differs: {sorted(set(members) ^ package_files(root))}"
            )
        for name, member in members.items():
            if member.issym():
                if name != "usr/bin/srvr" or member.linkname != "../lib/srvr/bin/srvr":
                    raise ValueError(f"unexpected package link: {name}")
            elif not member.isfile():
                raise ValueError(f"unexpected package entry type: {name}")
        check_packaged_source(archive, members, root)
        stamp = archive.extractfile(members["usr/lib/srvr/srvr/build.json"])
        if stamp is None:
            raise ValueError("missing build identity")
        return dict(json.loads(stamp.read()))


def bundle_names(root: Path) -> set[str]:
    release = version(root)
    return {
        f"srvr_{release}-1_all.deb",
        f"srvr-{release}-source.tar.gz",
        "RELEASE_NOTES.md",
        "PROVENANCE.json",
        "SHA256SUMS",
    }


def verify_bundle(output: Path, root: Path = ROOT) -> None:
    if {path.name for path in output.iterdir()} != bundle_names(root):
        raise ValueError("release bundle has missing or unexpected files")
    for path in output.iterdir():
        if not path.is_file() or path.is_symlink():
            raise ValueError("release bundle must contain regular files")
    lines = (output / "SHA256SUMS").read_text().splitlines()
    sums = dict(line.split("  ", 1)[::-1] for line in lines)
    if len(lines) != len(sums) or set(sums) != bundle_names(root) - {"SHA256SUMS"}:
        raise ValueError("checksum inventory differs")
    for name, digest in sums.items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"checksum mismatch: {name}")
    provenance = json.loads((output / "PROVENANCE.json").read_text())
    record = check_package(output / f"srvr_{version(root)}-1_all.deb", root)
    if record != {key: provenance[key] for key in record} or record["version"] != version(root):
        raise ValueError("package identity differs from provenance")
    if (root / ".git").exists() and record["revision"] != git("rev-parse", "HEAD", root=root):
        raise ValueError("release revision differs from checkout")
    with tarfile.open(output / f"srvr-{version(root)}-source.tar.gz") as archive:
        members = archive.getmembers()
        if sorted(member.name for member in members) != inventory(root):
            raise ValueError("source archive inventory differs")
        for member in members:
            content = archive.extractfile(member) if member.isfile() else None
            if content is None or content.read() != (root / member.name).read_bytes():
                raise ValueError(f"source archive differs: {member.name}")
    with tempfile.TemporaryDirectory(prefix="srvr-artifact-scan-") as directory:
        unpacked = Path(directory)
        data = deb_archives(output / f"srvr_{version(root)}-1_all.deb")["data.tar.xz"]
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(unpacked, filter="data")
        controls = deb_archives(output / f"srvr_{version(root)}-1_all.deb")["control.tar.xz"]
        with tarfile.open(fileobj=io.BytesIO(controls)) as archive:
            archive.extractall(unpacked / "DEBIAN", filter="data")
        for path in list(unpacked.rglob("*.gz")):
            path.with_suffix("").write_bytes(gzip.decompress(path.read_bytes()))
        scan(unpacked)
    scan(output)
    print("Release inventories, checksums, source contents, and build identity passed.")


def prepare(ref: str, output: Path) -> None:
    revision = git("rev-parse", f"{ref}^{{commit}}")
    if output.exists() and any(output.iterdir()):
        raise ValueError("release output must be empty; use a new directory")
    epoch = int(git("show", "-s", "--format=%ct", revision))
    with tempfile.TemporaryDirectory(prefix="srvr-release-") as directory:
        work = Path(directory)
        exported = work / "export.tar"
        run("git", "archive", "--format=tar", f"--output={exported}", revision)
        source = work / "source"
        source.mkdir()
        with tarfile.open(exported) as archive:
            archive.extractall(source, filter="data")
        inventory(source)
        product = version(source)
        if ref.startswith("v") and ref != f"v{product}":
            raise ValueError("tag does not match the product version")
        stamp = {
            "version": product,
            "revision": revision,
            "commit_time": datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z"),
            "build_time": datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z"),
        }
        (source / "srvr/build.json").write_text(json.dumps(stamp, indent=2) + "\n")
        environment = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
        run("scripts/verify", "--all", cwd=source, env=environment)
        output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            source / "dist" / f"srvr_{product}-1_all.deb", output / f"srvr_{product}-1_all.deb"
        )
        source_archive(source, output / f"srvr-{product}-source.tar.gz", epoch)
        url = tomllib.loads((source / "pyproject.toml").read_text())["project"]["urls"]["Source"]
        notes = (source / "docs/release-notes.md").read_text()
        notes = re.sub(
            r"\]\(([a-z-]+\.md)(#[^)]*)?\)", rf"]({url}/blob/v{product}/docs/\1\2)", notes
        )
        (output / "RELEASE_NOTES.md").write_text(notes)
        provenance = {
            **stamp,
            "source_tree": git("rev-parse", f"{revision}^{{tree}}"),
            "source_date_epoch": epoch,
            "builder": (source / "packaging/Dockerfile").read_text().splitlines()[0],
            "inventory_sha256": hashlib.sha256(
                (source / "packaging/source-files.txt").read_bytes()
            ).hexdigest(),
        }
        (output / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n")
        checksums = [
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
            for path in sorted(output.iterdir())
        ]
        (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
        verify_bundle(output, source)
        run("scripts/test-deb", str(output / f"srvr_{product}-1_all.deb"), cwd=source)
    print(f"Verified release bundle: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-source")
    commands.add_parser("stamp")
    staging = commands.add_parser("stage")
    staging.add_argument("--output", type=Path, required=True)
    package = commands.add_parser("check-package")
    package.add_argument("path", type=Path)
    bundle = commands.add_parser("verify-bundle")
    bundle.add_argument("--output", type=Path, required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--ref", default="HEAD")
    preparation.add_argument("--output", type=Path, default=ROOT / "dist/release")
    scanning = commands.add_parser("scan-range")
    scanning.add_argument("--before", required=True)
    scanning.add_argument("--after", required=True)
    args = parser.parse_args()
    if args.command == "check-source":
        check_source(ROOT)
    elif args.command == "stamp":
        (ROOT / "srvr/build.json").write_text(json.dumps(identity(ROOT), indent=2) + "\n")
    elif args.command == "stage":
        stage(ROOT, args.output.resolve())
    elif args.command == "check-package":
        check_package(args.path)
    elif args.command == "verify-bundle":
        verify_bundle(args.output.resolve())
    elif args.command == "prepare":
        prepare(args.ref, args.output.resolve())
    elif args.command == "scan-range":
        scan_range(args.before, args.after)


if __name__ == "__main__":
    main()
