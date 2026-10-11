from __future__ import annotations

import json
import os
import subprocess

from srvr.core.build import info
from srvr.core.shell import Shell
from tests.helpers import TempDirTest


class BuildTest(TempDirTest):
    def test_packaged_identity_needs_neither_git_nor_registry(self) -> None:
        (self.tmp / "srvr").mkdir()
        record = {
            "version": "0.1.0",
            "revision": "a" * 40,
            "commit_time": "2026-10-11T00:00:00Z",
            "build_time": "2026-10-11T00:00:00Z",
        }
        (self.tmp / "srvr/build.json").write_text(json.dumps(record))
        self.assertEqual(info(Shell(), self.tmp), record)

    def test_modified_source_does_not_claim_verified_revision(self) -> None:
        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", *args],
                cwd=self.tmp,
                text=True,
                env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
            ).strip()

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        source = self.tmp / "source.txt"
        source.write_text("committed\n")
        git("add", ".")
        git("commit", "-m", "Synthetic source")
        self.assertEqual(info(Shell(), self.tmp)["revision"], git("rev-parse", "HEAD"))
        (self.tmp / "srvr").mkdir()
        (self.tmp / "srvr/build.json").write_text(json.dumps({"revision": "stale"}))
        source.write_text("modified\n")
        self.assertEqual(info(Shell(), self.tmp)["revision"], "dev")
