"""Public CLI deployment coverage on uniquely owned Docker fixtures."""

import json
import os
import shlex
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from tests.docker_acceptance_support import DockerAcceptanceTest


@unittest.skipUnless(os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1", "Docker acceptance is opt-in")
class RegistryConfigurationTest(DockerAcceptanceTest):
    def test_env_override_batch_failure_and_configuration_rollback(self):
        refs, database = self.prepare_registry()
        self.srvr("stop", "example")
        file = self.work / "compose.yaml"
        file.write_text(file.read_text().replace(f"image: {refs[0]}", "image: ${APP_VERSION:?}"))
        self.env_file.write_text(self.env_file.read_text() + f"APP_VERSION={refs[0]}\n")
        override = self.work / "override.json"
        override.write_text(json.dumps({"services": {"app": {"image": "${APP_VERSION:?}"}}}))
        registration = self.tmp / "config" / "srvr" / "services" / "example.toml"
        registration.write_text(
            registration.read_text().replace(
                f'compose_files = ["{file}"]', f'compose_files = ["{file}", "{override}"]'
            )
        )
        self.srvr("start", "example")
        original = file.read_bytes()
        original_override = override.read_bytes()
        self.srvr("update", "example", "--set-image", f"app={refs[1]}", "-y")
        self.assertEqual(self.response()["version"], "two")
        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(override.read_bytes(), original_override)
        effective = json.loads(self.srvr("compose", "example", "config", "--format", "json").stdout)
        self.assertEqual(effective["services"]["app"]["image"], self.running_images()["app"])
        baseline = self.running_containers()
        before_images = self.running_images()
        failed = self.srvr(
            "update",
            "example",
            "--set-image",
            f"app={refs[0]}",
            "--set-image",
            f"db={database.rsplit(':', 1)[0]}:missing",
            "-y",
            check=False,
        )
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(self.running_containers(), baseline)
        self.assertEqual(self.running_images(), before_images)
        database_next = database.rsplit(":", 1)[0] + ":2"
        self.docker("image", "tag", self.base_id, database_next)
        self.images.add(database_next)
        self.docker("push", database_next)
        backup_dir = self.tmp / "share" / "srvr" / "backups" / "example"
        previous_backups = set(backup_dir.glob("*.tar.zst"))
        self.srvr(
            "update",
            "example",
            "--set-image",
            f"app={refs[0]}",
            "--set-image",
            f"db={database_next}",
            "-y",
        )
        self.assertEqual(self.response()["version"], "one")
        self.assertEqual(len(set(backup_dir.glob("*.tar.zst")) - previous_backups), 1)
        candidate = self.tmp / "candidate.env"
        candidate.write_text(self.env_file.read_text() + "CONFIG_MARKER=private-candidate-value\n")
        mode = self.env_file.stat().st_mode
        previous = self.env_file.read_bytes()
        changed = self.srvr(
            "update", "example", "--config-file", f"{self.env_file}={candidate}", "-y"
        )
        self.assertNotIn("private-candidate-value", changed.stdout + changed.stderr)
        self.assertEqual(self.env_file.stat().st_mode, mode)
        self.assertIn("CONFIG_MARKER", self.env_file.read_text())
        self.srvr("rollback", "example", "--data", "-y")
        self.assertEqual(self.env_file.read_bytes(), previous)
        self.assertEqual(self.response()["version"], "one")


@unittest.skipUnless(os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1", "Docker acceptance is opt-in")
class SourceSafetyTest(DockerAcceptanceTest):
    def setUp(self):
        super().setUp()
        settings = self.tmp / "config" / "srvr" / "srvr.toml"
        settings.write_text("health_timeout = 12\n")

    def compose_text(self):
        text = (
            super()
            .compose_text()
            .replace(
                "    profiles: [tools]\n",
                "    profiles: [tools]\n    depends_on:\n"
                "      db:\n        condition: service_healthy\n",
            )
        )
        return (
            text
            + """\
  worker:
    image: ${TEST_BASE_IMAGE:?}
    pull_policy: never
    command: [python, -c, 'import time; time.sleep(300)']
    profiles: [workers]
    stop_grace_period: 1s
    healthcheck:
      test: [CMD, python, -c, 'import time; time.sleep(0.1)']
      start_period: 2s
      interval: 1s
      timeout: 2s
      retries: 2
  disabled:
    image: ${TEST_BASE_IMAGE:?}
    pull_policy: never
    command: [python, -c, 'raise SystemExit(1)']
    profiles: [debug]
"""
        )

    def test_selected_worker_health_failure_rolls_back_while_http_was_successful(self):
        first = self.prepare_source("--profile", "workers")
        self.assertEqual(set(self.running_images()), {"app", "db", "worker"})
        worker = self.docker(
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
            "--filter",
            "label=com.docker.compose.service=worker",
        ).stdout.strip()
        self.docker("kill", worker)
        self.assertEqual(self.response()["version"], "one")
        self.assertEqual(self.srvr("health", "example", check=False).returncode, 1)
        self.srvr("start", "example")
        file = self.upstream / "compose.yaml"
        file.write_text(
            file.read_text().replace("import time; time.sleep(300)", "raise SystemExit(1)")
        )
        self.commit(version="worker-failure", schema=2)
        failed = self.srvr("update", "example", "-y", check=False)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), first)

        self.srvr("health", "example")


@unittest.skipUnless(os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1", "Docker acceptance is opt-in")
class NamedVolumeAcceptanceTest(DockerAcceptanceTest):
    def volume_fixture(self, *, migration=False):
        if migration:
            settings = self.tmp / "config" / "srvr" / "srvr.toml"
            settings.write_text("health_timeout = 12\nmigration_timeout = 8\n")
        self.volume = self.project + "-data"
        self.helper = self.project + "-storage"
        self.docker(
            "volume", "create", "--label", f"com.docker.compose.project={self.project}", self.volume
        )
        self.addCleanup(self.remove_volume)
        volume = json.loads(self.docker("volume", "inspect", self.volume).stdout)[0]
        self.mountpoint = Path(volume["Mountpoint"])
        root = Path(self.docker("info", "--format", "{{.DockerRootDir}}").stdout.strip())
        self.docker(
            "run",
            "--detach",
            "--name",
            self.helper,
            "--mount",
            f"type=bind,src={root},dst={root},readonly",
            "--mount",
            f"type=bind,src={self.mountpoint.parent},dst={self.mountpoint.parent}",
            "--mount",
            f"type=bind,src={self.tmp},dst={self.tmp}",
            self.base,
            "python",
            "-c",
            "import time; time.sleep(600)",
        )
        self.docker(
            "exec",
            self.helper,
            "sh",
            "-c",
            'printf before > "$1/marker"; chmod 600 "$1/marker"; chown 1234:2345 "$1/marker"',
            "sh",
            str(self.mountpoint),
        )
        adapter = self.tmp / "bin" / "sudo"
        adapter.write_text(
            '#!/bin/sh\ncase "$1" in\n-n|--) shift;;\n-v) exit 0;;\nesac\n'
            'case "$1" in\n'
            'docker|env|printenv) exec "$@";;\n'
            'test) if [ "$2" = -S ]; then exec "$@"; fi;;\n'
            "esac\nexec "
            + shlex.quote(self.docker_binary)
            + " exec -i "
            + shlex.quote(self.helper)
            + ' "$@"\n'
        )
        self.work.mkdir()
        file = self.work / "compose.json"
        file.write_text(
            json.dumps(
                {
                    "services": {
                        "app": {
                            "image": self.base,
                            "pull_policy": "never",
                            "command": ["python", "-c", "import time; time.sleep(300)"],
                            "stop_grace_period": "1s",
                            "volumes": ["database:/data"],
                        }
                    },
                    "volumes": {"database": {"name": self.volume, "external": True}},
                }
            )
        )
        options = []
        if migration:
            model = json.loads(file.read_text())
            model["services"]["migrate"] = {
                **model["services"]["app"],
                "profiles": ["tools"],
                "command": [
                    "python",
                    "-c",
                    "from pathlib import Path; import time; "
                    "Path('/data/marker').write_text('migrating'); time.sleep(300)",
                ],
            }
            file.write_text(json.dumps(model))
            options = ["--migrate", "migrate"]
        result = self.srvr(
            "add",
            "example",
            "--kind",
            "image",
            "--workdir",
            str(self.work),
            "--compose-file",
            str(file),
            "--project",
            self.project,
            "--data-volume",
            self.volume,
            "--start",
            *options,
            "-y",
            check=not migration,
        )
        return (file, result) if migration else file

    def remove_volume(self):
        self.docker(
            "exec", self.helper, "rm", "-rf", "--", str(self.tmp / "snapshots"), check=False
        )
        self.remove_resources()
        self.docker("rm", "--force", self.helper, check=False)
        self.docker("volume", "rm", self.volume, check=False)

    def storage(self, code):
        prefix = f"from pathlib import Path; p=Path({str(self.mountpoint)!r}); "
        return self.docker("exec", self.helper, "python", "-c", prefix + code).stdout.strip()

    def test_timed_out_migration_is_removed_before_registered_data_recovery(self):
        _, failed = self.volume_fixture(migration=True)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("timed out", failed.stderr)
        self.assertEqual(self.running_containers(), [])
        self.assertEqual(self.storage("print((p / 'marker').read_text())"), "before")
        retained = self.storage(
            "print(any((d / 'marker').read_text() == 'migrating' "
            "for d in p.parent.glob('_data.pre-restore-*')))"
        )
        self.assertEqual(retained, "True")
        oneoffs = self.docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
            "--filter",
            "label=com.docker.compose.oneoff=True",
        )
        self.assertEqual(oneoffs.stdout.strip(), "")

    def test_named_volume_configuration_rollback_preserves_data_modes_and_root_identity(self):
        file = self.volume_fixture()
        inode = self.storage(f"import os; print(os.stat({str(self.mountpoint)!r}).st_ino)")
        self.srvr("update", "example", "--capture-config", "-y")
        candidate = self.tmp / "candidate.json"
        model = json.loads(file.read_text())
        model["services"]["app"]["command"] = [
            "python",
            "-c",
            "from pathlib import Path; import time; "
            "Path('/data/marker').write_text('after'); time.sleep(300)",
        ]
        candidate.write_text(json.dumps(model))
        self.srvr("update", "example", "--config-file", f"{file}={candidate}", "-y")
        self.assertEqual(
            self.storage("print((p / 'marker').read_text())"),
            "after",
        )
        self.srvr("rollback", "example", "--data", "-y")
        state = self.storage(
            "import json; f=p / 'marker'; s=f.stat(); "
            "print(json.dumps([f.read_text(), s.st_uid,s.st_gid,s.st_mode & 511,p.stat().st_ino]))"
        )
        self.assertEqual(json.loads(state), ["before", 1234, 2345, 0o600, int(inode)])
        retained = self.storage("print(len(list(p.parent.glob('_data.pre-restore-*'))))")
        self.assertGreater(int(retained), 0)
        destination = self.tmp / "snapshots"
        self.srvr("snapshot", "example", "--to", str(destination))
        self.storage("(p / 'marker').write_text('snapshot-change')")
        self.srvr("restore", "example", "--snapshot", str(destination / "example"), "-y")
        self.assertEqual(self.storage("print((p / 'marker').read_text())"), "before")
        original = file.read_bytes()
        model = json.loads(original)
        model["services"]["app"]["command"] = [
            "python",
            "-c",
            "from pathlib import Path; Path('/data/marker').write_text('failed'); "
            "raise SystemExit(1)",
        ]
        candidate.write_text(json.dumps(model))
        failed = self.srvr(
            "update", "example", "--config-file", f"{file}={candidate}", "-y", check=False
        )
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(self.storage("print((p / 'marker').read_text())"), "before")
        self.srvr("health", "example")

    def test_interrupted_volume_restore_guards_start_restart_and_raw_compose(self):
        self.volume_fixture()
        self.srvr("backup", "example")
        self.srvr("stop", "example")
        self.storage("(p / 'marker').write_text('after')")
        binary = self.tmp / "bin" / "mv"
        binary.write_text('#!/bin/sh\nkill -KILL "$PPID"\nexit 1\n')
        binary.chmod(0o700)
        adapter = self.tmp / "bin" / "sudo"
        adapter.write_text(
            adapter.read_text().replace(
                'case "$1" in\ndocker',
                'case "$1" in\nmv) exec ' + shlex.quote(str(binary)) + ' "$@";;\ndocker',
            )
        )
        failed = self.srvr("restore", "example", "-y", check=False)
        self.assertNotEqual(failed.returncode, 0)
        journal = self.tmp / "share" / "srvr" / "backups" / "example" / ".restore.json"
        self.assertTrue(journal.exists())
        for command in (
            ("start", "example"),
            ("restart", "example"),
            ("compose", "example", "up", "-d"),
            ("compose", "example", "run", "--rm", "app"),
        ):
            result = self.srvr(*command, check=False)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("unfinished restore", result.stderr)
        self.assertEqual(self.running_containers(), [])
        binary.unlink()
        adapter.write_text(
            adapter.read_text().replace("mv) exec " + shlex.quote(str(binary)) + ' "$@";;\n', "")
        )
        identity = self.command(
            [
                self.python_binary,
                "-c",
                "import json; from srvr.core import volumes; from srvr.core.paths import Paths; "
                "from srvr.core.registry import load_registry; from srvr.core.shell import Shell; "
                "p=Paths.from_env(); s=load_registry(p.services_dir).services['example']; "
                "print(json.dumps(volumes.inspect(Shell(),s)[0].record()))",
            ],
            cwd=self.package_root,
        )
        recorded = json.loads(journal.read_text())["directories"][0]["volume"]
        current = json.loads(identity.stdout)
        self.assertEqual(
            {key: value for key, value in recorded.items() if key != "created_at"},
            {key: value for key, value in current.items() if key != "created_at"},
        )
        self.srvr("restore", "example", "-y")
        self.assertFalse(journal.exists())
        self.assertEqual(
            self.storage("print((p / 'marker').read_text())"),
            "before",
        )
        self.srvr("health", "example")


@unittest.skipUnless(os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1", "Docker acceptance is opt-in")
class AutomaticMigrationTest(DockerAcceptanceTest):
    def test_automatic_app_startup_migration_failure_restores_committed_data(self):
        first = self.prepare_source()
        registration = self.tmp / "config" / "srvr" / "services" / "example.toml"
        registration.write_text(registration.read_text().replace('migrate = "migrate"\n', ""))
        self.commit(version="startup-failure", schema=2, fail_migration=True)
        file = self.upstream / "app.py"
        file.write_text(
            file.read_text().replace(
                "    else:\n        HTTPServer",
                "    else:\n        migrate(SCHEMA_VERSION, FAIL_MIGRATION)\n        HTTPServer",
            )
        )
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "--quiet", "-m", "Migrate during application startup")
        failed = self.srvr("update", "example", "-y", check=False)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), first)
        kept = next(self.data.parent.glob("data.pre-restore-*"))
        with closing(sqlite3.connect(kept / "app.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT version FROM metadata").fetchone()[0], 2)


@unittest.skipUnless(os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1", "Docker acceptance is opt-in")
class ComposeSelectionTest(DockerAcceptanceTest):
    def simple_project(self, extra=None):
        self.work.mkdir()
        specs = {
            "app": {
                "image": self.base,
                "pull_policy": "never",
                "command": ["python", "-c", "import time; time.sleep(300)"],
                "stop_grace_period": "1s",
            }
        }
        specs["app"].update(extra or {})
        file = self.work / "compose.json"
        file.write_text(json.dumps({"services": specs}))
        return file

    def add_args(self, file):
        return [
            "add",
            "example",
            "--kind",
            "image",
            "--workdir",
            str(self.work),
            "--compose-file",
            str(file),
            "--project",
            self.project,
            "-y",
        ]

    def test_private_default_and_explicit_public_opt_in(self):
        file = self.simple_project(
            {"ports": [{"target": 8000, "host_ip": "0.0.0.0", "published": "0"}]}
        )
        self.assertEqual(self.srvr(*self.add_args(file), check=False).returncode, 1)
        self.srvr(*self.add_args(file), "--allow-public-ports", "--start")
        container = self.running_containers()[0]
        host = json.loads(self.docker("inspect", container).stdout)[0]["HostConfig"]
        self.assertEqual(host["PortBindings"]["8000/tcp"][0]["HostIp"], "0.0.0.0")

    def test_host_network_requires_registered_opt_in(self):
        file = self.simple_project({"network_mode": "host"})
        self.assertEqual(self.srvr(*self.add_args(file), check=False).returncode, 1)
        self.srvr(*self.add_args(file), "--allow-host-network", "--start")
        host = json.loads(self.docker("inspect", self.running_containers()[0]).stdout)[0][
            "HostConfig"
        ]
        self.assertEqual(host["NetworkMode"], "host")

    def test_deployment_recovery_blocks_mutations_until_public_rollback_succeeds(self):
        file = self.simple_project()
        self.srvr(*self.add_args(file), "--start")
        state = self.tmp / "state" / "srvr" / "example" / "last-deploy.json"
        state.write_text(
            json.dumps(
                {
                    "service": "example",
                    "time": "2026-10-14T00:00:00Z",
                    "status": "failed",
                    "recovery_required": True,
                    "before": {"images": self.running_images(), "compose_text": file.read_text()},
                }
            )
        )
        for command in (
            ("start", "example"),
            ("restart", "example"),
            ("compose", "example", "up", "-d"),
            ("compose", "example", "build"),
        ):
            result = self.srvr(*command, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("unfinished deployment recovery", result.stderr)
        self.srvr("compose", "example", "ps")
        self.srvr("rollback", "example", "-y")
        self.assertFalse(json.loads(state.read_text())["recovery_required"])
        self.srvr("health", "example")

    def test_git_prebuilt_only_configuration_stays_repository_owned(self):
        file = self.simple_project()
        self.git(self.work, "init", "--quiet", "--initial-branch=main")
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "--quiet", "-m", "Initial prebuilt stack")
        first = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.tmp, "clone", "--quiet", "--bare", str(self.work), str(self.upstream))
        self.git(self.work, "remote", "add", "origin", str(self.upstream))
        args = self.add_args(file)
        args[args.index("image")] = "git"
        self.srvr(*args, "--branch", "main")
        self.srvr("update", "example", "--force", "-y")
        model = json.loads(file.read_text())
        model["services"]["app"]["labels"] = {"srvr.fixture": "candidate"}
        model["services"]["worker"] = {**model["services"]["app"]}
        file.write_text(json.dumps(model))
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "--quiet", "-m", "Change repository configuration")
        self.git(self.work, "push", "--quiet", "origin", "main")
        target = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "checkout", "--quiet", "--detach", first)
        self.srvr("update", "example", "-y")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), target)
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")
        self.assertEqual(set(self.running_images()), {"app", "worker"})
        self.srvr("rollback", "example", "-y")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), first)
        self.assertEqual(set(self.running_images()), {"app"})
        workers = self.docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
            "--filter",
            "label=com.docker.compose.service=worker",
        )
        self.assertEqual(workers.stdout.strip(), "")
