"""Opt-in integration checks on an isolated local Docker project; no registry pulls."""

import io
import json
import os
import unittest
import uuid
from argparse import Namespace
from unittest import mock

from srvr import cli
from srvr.commands import add, rollback
from srvr.commands.update import deploy, plan_image
from srvr.core import deploys
from srvr.core.context import Context
from srvr.core.health import Probe
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest


@unittest.skipUnless(
    os.environ.get("SRVR_DOCKER_TESTS") == "1", "set SRVR_DOCKER_TESTS=1 for local Docker checks"
)
class DockerWorkflowTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.shell = NoSudoShell()
        self.shell.ensure_sudo = lambda: None
        self.project = "srvr-test-" + uuid.uuid4().hex[:12]
        self.tags = [f"{self.project}/{name}:1" for name in ("app", "db")]
        self.tags.append(f"{self.project}/app:2")
        self.ctx = Context(
            paths=self.paths, shell=self.shell, ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.data = self.tmp / "live" / "data"
        self.data.mkdir(parents=True)
        self.marker = self.data / "marker"
        self.marker.write_text("before")
        self.service_ = self.service(project=self.project, data=[str(self.data)])
        self.file = self.work / "compose.yaml"
        self.write_compose(self.tags[0])
        self.docker_cleaned = False
        self.addCleanup(self.cleanup_docker)
        self.old_id = self.shell.run(
            ["docker", "image", "inspect", "alpine:latest", "--format", "{{.Id}}"]
        ).stdout.strip()
        for tag in self.tags[:2]:
            self.shell.run(["docker", "image", "tag", self.old_id, tag])
        container = self.shell.run(
            [
                "docker",
                "create",
                "--name",
                self.project + "-image",
                "--label",
                "srvr-test-revision=2",
                "alpine:latest",
                "sleep",
                "300",
            ]
        ).stdout.strip()
        try:
            self.new_id = self.shell.run(
                ["docker", "commit", container, self.tags[2]]
            ).stdout.strip()
        finally:
            self.shell.run(["docker", "rm", container], check=False)
        self.ctx.compose(self.service_).up(pull=False)

    def write_compose(self, app_ref):
        self.file.write_text(
            f"services:\n  app:\n    image: {app_ref}\n    command: ['sleep', '300']\n"
            "    stop_grace_period: 1s\n"
            f"    volumes: [{json.dumps(str(self.data) + ':/data')}]\n"
            f"  db:\n    image: {self.tags[1]}\n    command: ['sleep', '300']\n"
            "    pull_policy: always\n    stop_grace_period: 1s\n"
        )

    def tearDown(self) -> None:
        try:
            self.cleanup_docker()
        finally:
            super().tearDown()

    def cleanup_docker(self):
        if self.docker_cleaned:
            return
        self.ctx.compose(self.service_).passthrough(["down", "--remove-orphans"])
        tags = [
            *self.tags,
            *(
                f"{namespace}/{self.project}-{name}:latest"
                for namespace in ("srvr-prev", "srvr-preparing", "srvr-ready")
                for name in ("app", "db")
            ),
        ]
        self.shell.run(["docker", "image", "rm", *tags], check=False)
        self.docker_cleaned = True

    def images(self):
        return deploys.capture(self.ctx, self.service_, preserve=False).images

    def update(self, *, fail=False):
        args = Namespace(image=self.tags[2], component="app", check=False, force=False)
        plan = plan_image(self.ctx, self.service_, args, named=True)
        calls = []
        run = self.shell.run

        def local_pull(argv, **kwargs):
            if "pull" in argv:
                calls.append(list(argv))
                return run(["true"])
            return run(argv, **kwargs)

        wait = deploys.wait_ready

        def readiness(ctx, service):
            if fail and self.images()["app"] == self.new_id:
                self.marker.write_text("failed-version-data")
                return Probe("app", False, "injected unhealthy update")
            return wait(ctx, service)

        with (
            mock.patch.object(self.shell, "run", side_effect=local_pull),
            mock.patch.object(deploys, "wait_ready", side_effect=readiness),
        ):
            result = deploy(self.ctx, self.service_, plan, backup=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2:], ["pull", "app"])
        return result

    def test_update_preserves_database_and_manual_rollback_survives_tag_drift(self) -> None:
        original = self.file.read_text()
        self.shell.run(["docker", "image", "tag", self.new_id, self.tags[1]])
        self.assertTrue(self.update())
        self.assertEqual(self.images(), {"app": self.new_id, "db": self.old_id})
        self.marker.write_text("after")
        self.ctx.service = lambda name: self.service_
        self.assertEqual(rollback.run(Namespace(service="demo", data=True, yes=True), self.ctx), 0)
        self.assertEqual(self.images(), {"app": self.old_id, "db": self.old_id})
        self.assertEqual(self.marker.read_text(), "before")
        self.assertEqual(self.file.read_text(), original)
        self.ctx.compose(self.service_).up(pull=False)
        self.assertEqual(self.images()["db"], self.old_id)
        previous = self.shell.run(
            [
                "docker",
                "image",
                "inspect",
                f"srvr-prev/{self.project}-app:latest",
                "--format",
                "{{.Id}}",
            ]
        ).stdout.strip()
        self.assertEqual(previous, self.old_id)

    def test_unhealthy_update_restores_images_and_data_automatically(self) -> None:
        original = self.file.read_text()
        self.assertFalse(self.update(fail=True))
        self.assertEqual(self.images(), {"app": self.old_id, "db": self.old_id})
        self.assertEqual(self.marker.read_text(), "before")
        kept = list(self.data.parent.glob("data.pre-restore-*"))
        self.assertEqual((kept[0] / "marker").read_text(), "failed-version-data")
        self.assertEqual(self.file.read_text(), original)
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "rolled-back")

    def test_digest_rollback_and_real_compose_registration(self) -> None:
        digest = json.loads(
            self.shell.run(
                ["docker", "image", "inspect", "alpine:latest", "--format", "{{json .RepoDigests}}"]
            ).stdout
        )[0]
        self.write_compose(digest)
        self.ctx.compose(self.service_).up(pull=False)
        original = self.file.read_text()
        parser = cli.build_parser(cli.discover())
        args = parser.parse_args(
            [
                "add",
                "demo",
                "--workdir",
                str(self.work),
                "--kind",
                "image",
                "--project",
                self.project,
                "--data",
                str(self.data),
                "--update-component",
                "app",
                "-y",
            ]
        )
        self.assertEqual(add.run(args, self.ctx), 0)
        self.assertEqual(self.file.read_text(), original)
        self.assertTrue(self.update())
        self.ctx.service = lambda name: self.service_
        self.assertEqual(rollback.run(Namespace(service="demo", data=False, yes=True), self.ctx), 0)
        self.assertEqual(self.file.read_text(), original)
        self.assertEqual(self.images()["app"], self.old_id)
