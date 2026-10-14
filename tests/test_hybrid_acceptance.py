"""Authoritative source and published-image modes using the same hybrid Compose project."""

import json
import os
import sqlite3
import unittest
from contextlib import closing
from copy import deepcopy

from tests.docker_acceptance_support import DOCKERFILE, FIXTURES, DockerAcceptanceTest


@unittest.skipUnless(os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1", "Docker acceptance is opt-in")
class HybridModeAcceptanceTest(DockerAcceptanceTest):
    def fixture(self):
        self.refs, database = self.prepare_registry(register=False)
        build = {
            "context": ".",
            "args": {
                "BASE_IMAGE": self.base,
                "APP_REVISION": "${APP_REVISION:-}",
                "APP_COMMIT_TIME": "${APP_COMMIT_TIME:-}",
                "APP_BUILD_TIME": "${APP_BUILD_TIME:-}",
            },
        }
        app = {
            "image": self.refs[0],
            "build": build,
            "pull_policy": "build",
            "user": f"{os.getuid()}:{os.getgid()}",
            "volumes": [f"{self.data}:/data"],
            "ports": [f"127.0.0.1:{self.port}:8000"],
            "depends_on": {"db": {"condition": "service_healthy"}},
            "stop_grace_period": "1s",
        }
        database_spec = {
            "image": database,
            "pull_policy": "never",
            "user": f"{os.getuid()}:{os.getgid()}",
            "volumes": [
                f"{self.data}:/data",
                f"{FIXTURES / 'database.py'}:/fixture/database.py:ro",
            ],
            "command": ["python", "/fixture/database.py"],
            "stop_grace_period": "1s",
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/state')",
                ],
                "interval": "1s",
                "timeout": "2s",
                "retries": 10,
            },
        }
        migrate = deepcopy(app)
        migrate.pop("ports")
        migrate.update(command=["python", "/fixture/app.py", "migrate"], profiles=["tools"])
        worker = {
            "image": self.refs[0],
            "build": build,
            "pull_policy": "always",
            "command": ["python", "-c", "import time; time.sleep(300)"],
            "environment": {"LITERAL": "keep$$dollar"},
            "profiles": ["workers"],
            "stop_grace_period": "1s",
        }
        self.model = {
            "services": {
                "app": app,
                "db": database_spec,
                "migrate": migrate,
                "worker": worker,
                "disabled": {"build": {"context": "missing"}, "profiles": ["debug"]},
            }
        }
        self.file = self.work / "compose.yaml"
        self.file.write_text(json.dumps(self.model))
        self.original = self.file.read_bytes()
        self.write_code(version="local-source")

    def write_code(self, *, version, schema=1, fail_migration=False):
        code = (FIXTURES / "app.py").read_text()
        code = code.replace('VERSION = "one"', f'VERSION = "{version}"')
        code = code.replace("SCHEMA_VERSION = 1", f"SCHEMA_VERSION = {schema}")
        code = code.replace("FAIL_MIGRATION = False", f"FAIL_MIGRATION = {fail_migration}")
        (self.work / "app.py").write_text(code)

    def register(self, kind, *options, check=True):
        args = [
            "add",
            "example",
            "--kind",
            kind,
            "--workdir",
            str(self.work),
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--data",
            str(self.data),
            "--migrate",
            "migrate",
            "--profile",
            "workers",
            "--health",
            f"http://127.0.0.1:{self.port}/health",
            "--start",
            "--backup",
            "-y",
        ]
        if kind == "git":
            args += [
                "--branch",
                "main",
                "--version-url",
                f"http://127.0.0.1:{self.port}/version.json",
            ]
        return self.srvr(*args, *options, check=check)

    def source_repository(self):
        (self.work / ".dockerignore").write_text(".git\n")
        self.git(self.work, "init", "--quiet", "--initial-branch=main")
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "--quiet", "-m", "Hybrid source")
        self.git(self.tmp, "clone", "--quiet", "--bare", str(self.work), str(self.upstream))
        self.git(self.work, "remote", "add", "origin", str(self.upstream))
        return self.git(self.work, "rev-parse", "HEAD")

    def publish_candidate(self, tag, *, schema=1, fail_migration=False):
        self.write_code(version=tag, schema=schema, fail_migration=fail_migration)
        ref = self.refs[0].rsplit(":", 1)[0] + ":" + tag
        (self.work / "Dockerfile").write_text(DOCKERFILE)
        self.docker(
            "build",
            "--quiet",
            "--build-arg",
            f"BASE_IMAGE={self.base}",
            "--tag",
            ref,
            str(self.work),
        )
        self.images.add(ref)
        self.docker("push", ref)
        return ref

    def record(self):
        return self.tmp / "state" / "srvr" / "example" / "last-deploy.json"

    def test_same_configuration_pulls_published_artifact_then_builds_local_source(self):
        self.fixture()
        self.register("image", "--update-component", "app")
        self.assertEqual(self.response()["version"], "one")
        self.assertEqual(set(self.running_images()), {"app", "db", "worker"})
        worker = self.docker(
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
            "--filter",
            "label=com.docker.compose.service=worker",
        ).stdout.strip()
        environment = json.loads(self.docker("inspect", worker).stdout)[0]["Config"]["Env"]
        self.assertIn("LITERAL=keep$dollar", environment)
        self.assertEqual(self.file.read_bytes(), self.original)
        published_reads = self.registry_manifest_reads("app", "1")
        self.assertGreater(published_reads, 0)
        self.srvr("stop", "example")
        containers = self.docker(
            "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={self.project}"
        ).stdout.split()
        self.docker("rm", *containers)
        (self.tmp / "config" / "srvr" / "services" / "example.toml").unlink()
        (self.tmp / "state" / "srvr" / "example" / "images.compose.json").unlink()
        first = self.source_repository()
        self.register("git")
        self.assertEqual(self.response()["version"], "local-source")
        self.assertEqual(self.response("/version.json")["revision"], first)
        self.assertEqual(self.registry_manifest_reads("app", "1"), published_reads)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")

    def test_image_mode_ignores_unavailable_build_inputs_and_pulls_selected_hybrids(self):
        self.fixture()
        for spec in self.model["services"].values():
            if "build" in spec:
                spec["build"] = {"context": "absent-source", "dockerfile": "missing"}
        self.file.write_text(json.dumps(self.model))
        original = self.file.read_bytes()
        for name in ("Dockerfile", "app.py", "database.py"):
            (self.work / name).unlink()
        self.docker("image", "rm", self.refs[0])
        self.register("image", "--update-component", "app")
        self.assertEqual(self.response()["version"], "one")
        baseline = self.running_images()
        self.srvr(
            "update",
            "example",
            "--set-image",
            f"app={self.refs[1]}",
            "--set-image",
            f"worker={self.refs[1]}",
            "-y",
        )
        self.assertEqual(self.response()["version"], "two")
        self.assertEqual(self.running_images()["db"], baseline["db"])
        self.assertEqual(self.running_images()["worker"], self.running_images()["app"])
        self.assertEqual(self.file.read_bytes(), original)
        self.srvr("rollback", "example", "--data", "-y")
        self.assertEqual(self.response()["version"], "one")
        self.assertEqual(self.running_images(), baseline)
        previous = self.record().read_bytes()
        containers = self.running_containers()
        missing = self.refs[0].rsplit(":", 1)[0] + ":missing"
        failed = self.srvr(
            "update", "example", "--component", "app", "--image", missing, "-y", check=False
        )
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(self.running_containers(), containers)
        self.assertEqual(self.record().read_bytes(), previous)
        self.assertEqual(self.file.read_bytes(), original)

    def test_image_migration_failure_and_manual_data_rollback_restore_captured_artifacts(self):
        self.fixture()
        good = self.publish_candidate("good", schema=2)
        bad = self.publish_candidate("bad", schema=2, fail_migration=True)
        (self.work / "Dockerfile").write_text("invalid build that must never run\n")
        self.register("image")
        initial = self.running_images()
        failed = self.srvr(
            "update",
            "example",
            "--set-image",
            f"app={bad}",
            "--set-image",
            f"migrate={bad}",
            "-y",
            check=False,
        )
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.running_images(), initial)
        self.assertEqual(self.file.read_bytes(), self.original)
        with closing(
            sqlite3.connect(next(self.data.parent.glob("data.pre-restore-*")) / "app.sqlite")
        ) as connection:
            self.assertEqual(connection.execute("SELECT version FROM metadata").fetchone()[0], 2)
        self.srvr(
            "update",
            "example",
            "--set-image",
            f"app={good}",
            "--set-image",
            f"migrate={good}",
            "-y",
        )
        self.assertEqual(self.response(), {"schema": 2, "notes": ["converted"], "version": "good"})
        self.srvr("rollback", "example", "--data", "-y")
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.running_images(), initial)
        self.assertEqual(self.file.read_bytes(), self.original)
        self.srvr("health", "example")

    def test_source_git_updates_never_fall_back_after_build_failure(self):
        self.fixture()
        first = self.source_repository()
        self.register("git")
        self.assertEqual(self.response()["version"], "local-source")
        self.write_code(version="local-two")
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "--quiet", "-m", "New local source")
        self.git(self.work, "push", "--quiet", "origin", "main")
        target = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "checkout", "--quiet", "--detach", first)
        self.srvr("update", "example", "-y")
        self.assertEqual(self.response()["version"], "local-two")
        self.assertEqual(self.response("/version.json")["revision"], target)
        previous = self.record().read_bytes()
        containers = self.running_containers()
        (self.work / "Dockerfile").write_text("INVALID Dockerfile\n")
        self.git(self.work, "checkout", "--quiet", "main")
        self.git(self.work, "add", ".")
        self.git(self.work, "commit", "--quiet", "-m", "Broken source build")
        self.git(self.work, "push", "--quiet", "origin", "main")
        self.git(self.work, "checkout", "--quiet", "--detach", target)
        failed = self.srvr("update", "example", "-y", check=False)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertEqual(self.running_containers(), containers)
        self.assertEqual(self.response()["version"], "local-two")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), target)
        self.assertEqual(self.record().read_bytes(), previous)
        self.srvr("rollback", "example", "--data", "-y")
        self.assertEqual(self.response()["version"], "local-source")
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), first)
        self.assertEqual(self.file.read_bytes(), self.original)
