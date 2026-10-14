"""Public CLI acceptance tests with real builds, SQLite, migrations and health checks."""

import json
import os
import sqlite3
import unittest
from contextlib import closing

from tests.docker_acceptance_support import DockerAcceptanceTest


@unittest.skipUnless(
    os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1",
    "set SRVR_ACCEPTANCE_TESTS=1 for Docker acceptance tests",
)
class SourceDeploymentTest(DockerAcceptanceTest):
    def test_source_update_rollback_and_update_again_serve_the_correct_commit(self):
        first = self.prepare_source()
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.response("/version.json")["revision"], first)
        runtime = self.tmp / "state" / "srvr" / "example" / "images.compose.json"
        initial_migration = json.loads(runtime.read_text())["services"]["migrate"]["image"]
        second = self.commit(version="two", schema=2)
        self.srvr("update", "example", "-y")
        self.assertEqual(self.response(), {"schema": 2, "notes": ["converted"], "version": "two"})
        self.assertEqual(self.response("/version.json")["revision"], second)
        self.srvr("rollback", "example", "--data", "-y")
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.response("/version.json")["revision"], first)
        self.assertEqual(
            json.loads(runtime.read_text())["services"]["migrate"]["image"], initial_migration
        )
        self.srvr("update", "example", "-y")
        self.assertEqual(self.response(), {"schema": 2, "notes": ["converted"], "version": "two"})
        self.assertEqual(self.response("/version.json")["revision"], second)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), second)
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")
        self.srvr("health", "example")

    def test_failed_migration_restores_database_schema_data_and_serving_app(self):
        first = self.prepare_source()
        self.commit(version="broken", schema=2, fail_migration=True)
        result = self.srvr("update", "example", "-y", check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        kept = next(self.data.parent.glob("data.pre-restore-*"))
        with closing(sqlite3.connect(kept / "app.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT version FROM metadata").fetchone()[0], 2)
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.response("/version.json")["revision"], first)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), first)
        self.assertEqual(self.git(self.work, "status", "--porcelain"), "")
        self.srvr("health", "example")

    def test_unhealthy_app_restores_committed_migration_and_previous_build(self):
        first = self.prepare_source()
        self.commit(version="unhealthy", schema=2, fail_health=True)
        result = self.srvr("update", "example", "-y", check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("HTTP 503", result.stderr)
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.assertEqual(self.response("/version.json")["revision"], first)
        self.assertEqual(self.git(self.work, "rev-parse", "HEAD"), first)
        self.srvr("health", "example")


@unittest.skipUnless(
    os.environ.get("SRVR_ACCEPTANCE_TESTS") == "1",
    "set SRVR_ACCEPTANCE_TESTS=1 for Docker acceptance tests",
)
class RegistryDeploymentTest(DockerAcceptanceTest):
    def test_real_pull_selects_only_the_app_and_failed_pull_preserves_running_installation(self):
        refs, database = self.prepare_registry()
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.docker("image", "tag", refs[1], database)
        self.docker("push", database)
        self.docker("image", "tag", self.base_id, database)
        expected = self.running_images()["db"]
        self.docker("image", "rm", refs[1])
        self.assertNotEqual(self.docker("image", "inspect", refs[1], check=False).returncode, 0)
        db_reads = self.registry_manifest_reads("db", "1")
        self.assertGreater(db_reads, 0)
        app_reads = self.registry_manifest_reads("app", "2")
        self.srvr("update", "example", "--component", "app", "--image", refs[1], "-y")
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "two"})
        self.assertEqual(self.running_images()["db"], expected)
        self.assertGreater(self.registry_manifest_reads("app", "2"), app_reads)
        self.assertEqual(self.registry_manifest_reads("db", "1"), db_reads)
        compose = (self.work / "compose.yaml").read_text()
        containers = self.running_containers()
        previous_image = self.docker(
            "image", "inspect", f"srvr-prev/{self.project}-app:latest", "--format", "{{.Id}}"
        ).stdout.strip()
        result = self.srvr(
            "update",
            "example",
            "--component",
            "app",
            "--image",
            refs[1].rsplit(":", 1)[0] + ":missing",
            "-y",
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.work / "compose.yaml").read_text(), compose)
        self.assertEqual(self.running_containers(), containers)
        self.assertEqual(
            self.docker(
                "image", "inspect", f"srvr-prev/{self.project}-app:latest", "--format", "{{.Id}}"
            ).stdout.strip(),
            previous_image,
        )
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "two"})
        self.srvr("health", "example")
        self.srvr("rollback", "example", "--data", "-y")
        self.assertEqual(self.response(), {"schema": 1, "notes": ["kept"], "version": "one"})
        self.srvr("health", "example")
