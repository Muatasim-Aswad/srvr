"""CLI concurrency checks with real Git, locks, and disposable process boundaries."""

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

from srvr.core import deploys
from srvr.core.lock import service_lock
from srvr.core.paths import Paths
from tests.helpers import TempDirTest
from tests.test_git import git

ROOT = Path(__file__).resolve().parent.parent


class ConcurrentCliTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.paths = Paths(
            self.paths.config / "srvr", self.paths.state / "srvr", self.paths.data / "srvr"
        )
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.file = self.work / "compose.yaml"
        self.file.write_text("services:\n  app:\n    image: example/app:1\n")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "docker-calls"
        docker = self.bin / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"with open({str(self.calls)!r}, 'a') as log:\n"
            " log.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1] == 'ps':\n"
            " print('demo\\tapp\\tdemo-app\\trunning\\tUp 1 second\\tcid')\n"
            "elif 'config' in sys.argv:\n"
            " print(json.dumps({'services': {'app': {'image': 'example/app:1'}}}))\n"
        )
        docker.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = -n ]; then exit 0; fi\n'
            'if [ "$1" = -- ]; then shift; fi\nexec "$@"\n'
        )
        sudo.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "XDG_CONFIG_HOME": str(self.paths.config.parent),
            "XDG_STATE_HOME": str(self.paths.state.parent),
            "XDG_DATA_HOME": str(self.paths.data.parent),
            "PYTHONUNBUFFERED": "1",
        }

    def register(self, *, kind="image") -> None:
        self.paths.services_dir.mkdir(parents=True, exist_ok=True)
        branch = 'branch = "main"\n' if kind == "git" else ""
        (self.paths.services_dir / "demo.toml").write_text(
            f'kind = "{kind}"\nworkdir = "{self.work}"\n{branch}'
        )

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "srvr", *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def awaiting_confirmation(self, *args):
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, "-m", "srvr", *args],
            cwd=ROOT,
            env=self.env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave)
        self.addCleanup(os.close, master)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        output = b""
        deadline = time.monotonic() + 10
        while b"[y/N]" not in output:
            if time.monotonic() > deadline or process.poll() is not None:
                _, error = process.communicate(timeout=5)
                self.fail(f"CLI did not reach confirmation: {output!r}; {error!r}")
            if select.select([process.stdout], [], [], 0.1)[0]:
                output += os.read(process.stdout.fileno(), 4096)
        return process, master

    def test_source_update_rejects_a_checkout_changed_during_confirmation(self) -> None:
        upstream = self.tmp / "upstream"
        upstream.mkdir()
        git(upstream, "init", "--quiet", "--initial-branch=main")

        def commit(value):
            (upstream / "file").write_text(value)
            git(upstream, "add", "file")
            git(upstream, "commit", "--quiet", "-m", value)
            return git(upstream, "rev-parse", "HEAD")

        commit("one")
        self.file.unlink()
        self.work.rmdir()
        git(self.tmp, "clone", "--quiet", str(upstream), str(self.work))
        commit("two")
        self.register(kind="git")
        process, terminal = self.awaiting_confirmation("update", "demo")
        latest = commit("three")
        git(self.work, "fetch", "--quiet", "origin")
        git(self.work, "reset", "--hard", "--quiet", latest)
        os.write(terminal, b"y\n")
        _, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, error.decode())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), latest)
        self.assertFalse(self.calls.exists())

    def test_rollback_rejects_a_deployment_record_changed_during_confirmation(self) -> None:
        self.register()
        before = deploys.Snapshot(image_refs={"app": "example/app:0"}, compose_text="old")
        record = deploys.DeployRecord("demo", "first", "ok", before)
        deploys.save_record(self.paths, record)
        process, terminal = self.awaiting_confirmation("rollback", "demo")
        replacement = deploys.DeployRecord("demo", "newer", "ok", before)
        deploys.save_record(self.paths, replacement)
        original = self.file.read_text()
        self.calls.unlink()
        os.write(terminal, b"y\n")
        _, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, error.decode())
        self.assertEqual(deploys.load_record(self.paths, "demo"), replacement)
        self.assertEqual(self.file.read_text(), original)
        self.assertFalse(self.calls.exists())

    def test_lifecycle_commands_cannot_mutate_a_locked_service(self) -> None:
        self.register()
        with service_lock(self.paths, "demo"):
            for command in ("start", "stop", "restart"):
                with self.subTest(command=command):
                    result = self.cli(command, "demo")
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("another srvr command", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_compose_mutations_and_prune_cannot_bypass_service_locks(self) -> None:
        self.register()
        with service_lock(self.paths, "demo"):
            for args in (("compose", "demo", "up", "-d"), ("prune", "-y")):
                with self.subTest(args=args):
                    result = self.cli(*args)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("another srvr command", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_unfinished_restore_blocks_start_and_restart(self) -> None:
        self.register()
        directory = self.paths.service_backups("demo")
        directory.mkdir(parents=True)
        (directory / ".restore.json").write_text("{}")
        for command in ("start", "restart"):
            with self.subTest(command=command):
                result = self.cli(command, "demo")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("unfinished restore", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_source_update_rejects_env_changes_during_confirmation(self) -> None:
        git(self.work, "init", "--quiet", "--initial-branch=main")
        git(self.work, "add", "compose.yaml")
        git(self.work, "commit", "--quiet", "-m", "one")
        upstream = self.tmp / "upstream.git"
        git(self.tmp, "clone", "--quiet", "--bare", str(self.work), str(upstream))
        git(self.work, "remote", "add", "origin", str(upstream))
        env_file = self.work / ".env"
        (self.work / ".gitignore").write_text(".env\n")
        git(self.work, "add", ".gitignore")
        git(self.work, "commit", "--quiet", "-m", "two")
        git(self.work, "push", "--quiet", "origin", "main")
        env_file.write_text("SETTING=before\n")
        self.register(kind="git")
        process, terminal = self.awaiting_confirmation("update", "demo", "--force")
        env_file.write_text("SETTING=after\n")
        os.write(terminal, b"y\n")
        _, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, error.decode())
        self.assertIn("configuration changed", error.decode())
        self.assertFalse(self.calls.exists())
