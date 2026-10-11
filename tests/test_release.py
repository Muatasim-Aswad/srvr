"""Source exports must exclude local files and refuse mismatched release identities."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.helpers import TempDirTest

ROOT = Path(__file__).resolve().parent.parent


class ReleaseTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.tmp / "source"
        self.source.mkdir()
        self.environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        files = {
            "pyproject.toml": '[project]\nversion = "0.1.0"\n',
            "srvr/__init__.py": '"""Synthetic package."""\n',
            "debian/changelog": (
                "srvr (0.1.0-1) unstable; urgency=medium\n\n  * Test.\n\n"
                " -- Test <test@example.invalid>  Sun, 11 Oct 2026 00:00:00 +0000\n"
            ),
        }
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (self.source / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/release.py", self.source / "scripts/release.py")
        (self.source / "packaging").mkdir()
        names = sorted([*files, "scripts/release.py", "packaging/source-files.txt"])
        (self.source / "packaging/source-files.txt").write_text("\n".join(names) + "\n")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "Initial synthetic source")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=self.source, text=True, env=self.environment
        ).strip()

    def release(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/release.py", *args],
            cwd=self.source,
            capture_output=True,
            text=True,
            check=False,
            env=self.environment,
        )

    def test_stage_excludes_untracked_files_and_keeps_committed_identity(self) -> None:
        (self.source / ".env").write_text("synthetic local configuration\n")
        (self.source / "srvr/private.local.md").write_text("private notes\n")
        output = self.tmp / "export"
        result = self.release("stage", "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((output / ".env").exists())
        self.assertFalse((output / "srvr/private.local.md").exists())
        self.assertFalse((output / ".git").exists())
        self.assertEqual(
            json.loads((output / "srvr/build.json").read_text())["revision"],
            self.git("rev-parse", "HEAD"),
        )

    def test_stage_refuses_tracked_files_outside_inventory(self) -> None:
        (self.source / "unreviewed.txt").write_text("not reviewed\n")
        self.git("add", "unreviewed.txt")
        result = self.release("stage", "--output", str(self.tmp / "export"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked files differ", result.stderr)

    def test_stage_refuses_symlinks_in_inventory(self) -> None:
        source = self.source / "srvr/__init__.py"
        source.unlink()
        source.symlink_to(self.source / "pyproject.toml")
        result = self.release("stage", "--output", str(self.tmp / "export"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe or missing source file", result.stderr)

    def test_mismatched_tag_is_refused_before_building(self) -> None:
        self.git("tag", "v9.0.0")
        result = self.release("prepare", "--ref", "v9.0.0", "--output", str(self.tmp / "bundle"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tag does not match", result.stderr)
        self.assertFalse((self.tmp / "bundle").exists())
