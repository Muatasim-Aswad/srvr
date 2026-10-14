import io
import json
from argparse import Namespace
from unittest import mock

from srvr.commands.update import Plan, deploy, plan_image, validate_selection
from srvr.core import deploys
from srvr.core.compose import Compose
from srvr.core.composefile import find_image
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.health import Probe
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest


class ImagePlanTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.file = self.work / "compose.yaml"
        self.original = (
            "services:\n  app:\n    image: example/app:1\n  db:\n    image: postgres:17\n"
        )
        self.file.write_text(self.original)
        self.service_ = self.service()
        self.ctx = mock.Mock()
        self.ctx.paths = self.paths
        self.compose = self.ctx.compose.return_value
        self.compose.config.side_effect = self.model

    def model(self, **_kwargs):
        return {
            "services": {
                name: {"image": find_image(self.file.read_text(), name)[1]}
                for name in ("app", "db")
            }
        }

    def args(self, **values):
        return Namespace(image="example/app:2", component="app", check=False, force=False, **values)

    def test_activation_edits_only_selected_literal(self) -> None:
        plan = plan_image(self.ctx, self.service_, self.args(), named=True)
        plan.validate()
        plan.activate()
        self.assertEqual(find_image(self.file.read_text(), "app")[1], "example/app:2")
        self.assertEqual(find_image(self.file.read_text(), "db")[1], "postgres:17")
        self.compose.pull.assert_not_called()
        plan.revert()
        self.assertEqual(self.file.read_text(), self.original)

    def test_ambiguous_stack_needs_selection_but_check_lists_all_pins(self) -> None:
        args = self.args()
        args.component = None
        with self.assertRaisesRegex(SrvrError, "choose --component"):
            plan_image(self.ctx, self.service_, args, named=True)
        args.image, args.check = None, True
        self.assertIsNone(plan_image(self.ctx, self.service_, args, named=False))
        self.assertIn("db=postgres:17", self.ctx.ui.line.call_args.args[0])

    def test_registry_default_can_be_overridden(self) -> None:
        service = self.service(update_component="app")
        args = self.args()
        args.component = None
        self.assertEqual(plan_image(self.ctx, service, args, named=True).component, "app")
        args.component, args.image = "db", "postgres:18"
        self.assertEqual(plan_image(self.ctx, service, args, named=True).component, "db")

    def test_accepts_hybrid_and_rejects_missing_component(self) -> None:
        args = self.args()
        args.component = "missing"
        with self.assertRaisesRegex(SrvrError, "not an image component"):
            plan_image(self.ctx, self.service_, args, named=True)
        model = self.model()
        model["services"]["app"]["build"] = {}
        self.compose.config.side_effect = None
        self.compose.config.return_value = model
        args.component = "app"
        self.assertIsNotNone(plan_image(self.ctx, self.service_, args, named=True))

    def test_overridden_image_uses_owned_override_and_stale_plans_are_rejected(self) -> None:
        model = self.model()
        model["services"]["app"]["image"] = "example/app:override"
        self.compose.config.side_effect = lambda **_kwargs: model
        self.assertIsNotNone(plan_image(self.ctx, self.service_, self.args(), named=True))
        self.assertEqual(self.file.read_text(), self.original)
        self.compose.config.side_effect = self.model
        plan = plan_image(self.ctx, self.service_, self.args(), named=True)
        self.file.write_text(self.original + "# concurrent edit\n")
        with self.assertRaisesRegex(SrvrError, "configuration changed"):
            plan.validate()
        self.compose.pull.assert_not_called()

    def test_shadowed_edit_is_rejected_before_pulling(self) -> None:
        model = self.model()
        self.compose.config.side_effect = lambda **_kwargs: model
        self.ctx.shell.run.return_value.stdout = json.dumps(model)
        plan = plan_image(self.ctx, self.service_, self.args(), named=True)
        with self.assertRaisesRegex(SrvrError, "did not change only"):
            plan.prepare()
        self.compose.pull.assert_not_called()
        plan.revert()
        self.assertEqual(self.file.read_text(), self.original)

    def test_git_component_selection_is_rejected(self) -> None:
        args = Namespace(ref=None, image=None, component="app")
        with self.assertRaisesRegex(SrvrError, "Git updates"):
            validate_selection(args, [self.service(kind="git", branch="main")])


class DeploymentRecoveryTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.ctx = Context(
            paths=self.paths, shell=NoSudoShell(), ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        self.service_ = self.service()
        self.service_.workdir.mkdir()
        self.service_.compose_files[0].write_text("services:\n  app:\n    image: app:1\n")
        self.compose = mock.Mock()
        self.compose.image_refs.return_value = {"app": "app:2", "db": "db:1"}
        self.compose.config.return_value = {
            "services": {"app": {"image": "app:2"}, "db": {"image": "db:1"}}
        }
        self.ctx.compose = mock.Mock(return_value=self.compose)
        self.ctx.docker = mock.Mock()
        self.ctx.docker.project_containers.return_value = []
        self.snapshot = deploys.Snapshot(
            images={"app": "sha256:old-app", "db": "sha256:old-db"},
            image_refs={"app": "app:1", "db": "db:1"},
            compose_text=self.service_.compose_files[0].read_text(),
        )
        self.plan = Plan("app:1", "app:2", mock.Mock(), mock.Mock(), component="app")

    def test_success_keeps_database_image_and_disables_implicit_pulls(self) -> None:
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(deploys, "capture", return_value=self.snapshot),
            mock.patch.object(deploys, "wait_ready", return_value=Probe("app", True, "ready")),
        ):
            self.assertTrue(deploy(self.ctx, self.service_, self.plan, backup=False))
        self.compose.pin_images.assert_called_once_with({"db": "sha256:old-db"})
        self.compose.up.assert_called_once_with(pull=False)
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "ok")

    def test_unhealthy_update_restores_previous_image_ids(self) -> None:
        self.plan.prepare.side_effect = lambda: self.service_.compose_files[0].write_text("changed")
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(deploys, "capture", return_value=self.snapshot),
            mock.patch.object(
                deploys,
                "wait_ready",
                side_effect=[Probe("app", False, "bad"), Probe("app", True, "ready")],
            ),
        ):
            self.assertFalse(deploy(self.ctx, self.service_, self.plan, backup=False))
        self.assertEqual(self.service_.compose_files[0].read_text(), self.snapshot.compose_text)
        self.compose.pin_images.assert_called_with(self.snapshot.images)
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "rolled-back")

    def test_failed_pull_reverts_configuration_and_preserves_running_images(self) -> None:
        self.plan.prepare.side_effect = SrvrError("pull failed")
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(deploys, "capture", return_value=self.snapshot),
            self.assertRaisesRegex(SrvrError, "preparation failed"),
        ):
            deploy(self.ctx, self.service_, self.plan, backup=False)
        self.plan.revert.assert_called_once()
        self.compose.pin_images.assert_called_once_with(self.snapshot.images)
        self.compose.up.assert_not_called()

    def test_digest_rollback_uses_state_override_without_retagging(self) -> None:
        self.snapshot.image_refs["app"] = "app@sha256:" + "a" * 64
        with mock.patch.object(self.ctx, "docker") as docker:
            deploys.apply(self.ctx, self.service_, self.snapshot)
        docker.tag.assert_not_called()
        self.compose.pin_images.assert_called_once_with(self.snapshot.images)
        self.compose.up.assert_called_once_with(pull=False)

    def test_git_rollback_and_new_deploy_keep_source_files_unchanged(self) -> None:
        service = self.service(kind="git", branch="main")
        for arguments in (
            ["init", "--quiet"],
            ["config", "user.email", "fixture@example.invalid"],
            ["config", "user.name", "Fixture"],
            ["add", "."],
            ["commit", "--quiet", "-m", "fixture"],
        ):
            self.ctx.shell.run(["git", "-C", str(service.workdir), *arguments])
        self.snapshot.commit = self.ctx.checkout(service).head()
        original = service.compose_files[0].read_text()
        with mock.patch.object(self.ctx, "checkout") as checkout:
            deploys.apply(self.ctx, service, self.snapshot)
        checkout.return_value.move_to.assert_called_once_with(self.snapshot.commit)
        self.compose.pin_images.assert_called_with(self.snapshot.images)
        self.compose.reset_mock()
        plan = Plan("old", "new", mock.Mock(), mock.Mock())
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(deploys, "capture", return_value=self.snapshot),
            mock.patch.object(deploys, "wait_ready", return_value=Probe("app", True, "ready")),
        ):
            self.assertTrue(deploy(self.ctx, service, plan, backup=False))
        self.compose.pin_images.assert_called_once_with({})
        self.assertEqual(service.compose_files[0].read_text(), original)

    def test_filesystem_failure_during_switch_rolls_back(self) -> None:
        self.compose.up.side_effect = [OSError("disk failure"), None]
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(deploys, "capture", return_value=self.snapshot),
            mock.patch.object(deploys, "wait_ready", return_value=Probe("app", True, "ready")),
        ):
            self.assertFalse(deploy(self.ctx, self.service_, self.plan, backup=False))
        self.compose.pin_images.assert_called_with(self.snapshot.images)
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "rolled-back")

    def test_failed_rollback_health_remains_retryable(self) -> None:
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(deploys, "capture", return_value=self.snapshot),
            mock.patch.object(deploys, "wait_ready", return_value=Probe("app", False, "bad")),
        ):
            self.assertFalse(deploy(self.ctx, self.service_, self.plan, backup=False))
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "failed")

    def test_runtime_pins_persist_but_planning_building_and_pulling_use_base_files(self) -> None:
        runtime = self.paths.service_state("demo") / "images.compose.json"
        shell = mock.Mock()
        shell.run.return_value.stdout = '{"services": {"app": {"image": "app:1"}}}'
        compose = Compose(self.service_, shell, runtime_file=runtime)
        compose.pin_images(self.snapshot.images)
        self.assertEqual(
            json.loads(runtime.read_text())["services"]["app"]["image"], "sha256:old-app"
        )
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(runtime), compose.argv("up"))
        for action in (compose.config, compose.build, lambda: compose.pull("app")):
            action()
            self.assertNotIn(str(runtime), shell.run.call_args.args[0])
        for command in ("build", "pull"):
            compose.passthrough([command])
            self.assertNotIn(str(runtime), shell.run.call_args.args[0])
        compose.pin_images({})
        self.assertNotIn(str(runtime), compose.argv("up"))
