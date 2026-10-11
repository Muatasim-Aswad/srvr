import io
from unittest import mock

from srvr import cli
from srvr.commands import add
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.onboarding import save_registration, validate_model
from srvr.core.registry import load_registry
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest


class AddTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.compose_file = self.work / "compose.yaml"
        self.original = "services:\n  app:\n    image: example/app:1.0\n"
        self.compose_file.write_text(self.original)
        self.env = self.work / "config.env"
        self.env.write_text("TOKEN=never-print-this-secret\n")
        self.data = self.tmp / "data" / "app"
        self.data.mkdir(parents=True)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.ctx = Context(paths=self.paths, shell=NoSudoShell(), ui=UI(out=self.out, err=self.err))
        self.model = {"services": {"app": {"image": "example/app:1.0"}}}
        self.compose = mock.Mock()
        self.compose.config.side_effect = lambda **_: self.model
        self.ctx.compose = mock.Mock(return_value=self.compose)
        self.ctx.docker = mock.Mock()
        self.ctx.docker.project_containers.return_value = []
        self.parser = cli.build_parser(cli.discover())

    def args(self, *extra: str):
        return self.parser.parse_args(
            ["add", "demo", "--workdir", str(self.work), "--kind", "image", "-y", *extra]
        )

    def test_registers_without_editing_project_or_printing_secrets(self) -> None:
        args = self.args("--env-file", "config.env", "--data", str(self.data))
        self.assertEqual(add.run(args, self.ctx), 0)
        service = load_registry(self.paths.services_dir).services["demo"]
        self.assertEqual(service.env_file, self.env)
        self.assertEqual(service.data, (self.data,))
        self.assertEqual(service.config_files, (self.env,))
        self.assertEqual(self.compose_file.read_text(), self.original)
        self.assertEqual(self.env.read_text(), "TOKEN=never-print-this-secret\n")
        self.assertNotIn("never-print-this-secret", self.out.getvalue() + self.err.getvalue())
        self.assertEqual(service.source.stat().st_mode & 0o777, 0o600)
        self.compose.up.assert_not_called()
        self.compose.pull.assert_not_called()

    def test_public_port_is_rejected_before_registration(self) -> None:
        self.model["services"]["app"]["ports"] = [{"host_ip": "0.0.0.0", "published": "80"}]
        with self.assertRaisesRegex(SrvrError, "loopback|127.0.0.1"):
            add.run(self.args(), self.ctx)
        self.assertFalse(self.paths.services_dir.exists())

    def test_missing_data_is_not_created_or_registered(self) -> None:
        missing = self.tmp / "missing"
        with self.assertRaisesRegex(SrvrError, "prepare the data"):
            add.run(self.args("--data", str(missing)), self.ctx)
        self.assertFalse(missing.exists())
        self.assertFalse(self.paths.services_dir.exists())

    def test_duplicate_registration_is_preserved(self) -> None:
        args = self.args()
        add.run(args, self.ctx)
        target = self.paths.services_dir / "demo.toml"
        original = target.read_text()
        with self.assertRaisesRegex(SrvrError, "already registered"):
            add.run(args, self.ctx)
        self.assertEqual(target.read_text(), original)

    def test_cancellation_saves_nothing(self) -> None:
        args = self.args()
        args.yes = False
        with (
            mock.patch("sys.stdin.isatty", return_value=False),
            self.assertRaisesRegex(SrvrError, "confirmation required"),
        ):
            add.run(args, self.ctx)
        self.assertFalse(self.paths.services_dir.exists())

    def test_same_project_cannot_be_registered_under_another_name(self) -> None:
        add.run(self.args(), self.ctx)
        args = self.args()
        args.service = "other"
        with self.assertRaisesRegex(SrvrError, "already registered as demo"):
            add.run(args, self.ctx)
        self.assertFalse((self.paths.services_dir / "other.toml").exists())

    def test_running_project_is_registered_without_redeploying(self) -> None:
        self.ctx.docker.project_containers.return_value = [mock.Mock(state="running")]
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            self.assertRaisesRegex(SrvrError, "already running"),
        ):
            add.run(self.args("--start"), self.ctx)
        self.compose.build.assert_not_called()
        self.compose.pull.assert_not_called()
        self.compose.up.assert_not_called()
        self.assertIn("demo", load_registry(self.paths.services_dir).services)

    def test_git_registration_keeps_source_and_override_unchanged(self) -> None:
        override = self.work / "compose.server.yaml"
        override.write_text("services:\n  app:\n    ports: ['127.0.0.1:8080:8080']\n")
        self.model["services"]["app"]["build"] = {"context": str(self.work)}
        checkout = mock.Mock()
        checkout.is_clean.return_value = True
        self.ctx.checkout = mock.Mock(return_value=checkout)
        args = self.args(
            "--branch",
            "dev",
            "--compose-file",
            "compose.yaml",
            "--compose-file",
            "compose.server.yaml",
        )
        args.kind = "git"
        self.assertEqual(add.run(args, self.ctx), 0)
        service = load_registry(self.paths.services_dir).services["demo"]
        self.assertEqual(service.compose_files, (self.compose_file, override))
        checkout.resolve.assert_called_once_with("dev")
        checkout.move_to.assert_not_called()
        self.assertEqual(self.compose_file.read_text(), self.original)
        self.assertIn("127.0.0.1", override.read_text())

    def test_interactive_registration_accepts_bind_mount_default(self) -> None:
        self.model["services"]["app"]["volumes"] = [{"type": "bind", "source": str(self.data)}]
        args = self.args()
        args.yes = False
        with (
            mock.patch("sys.stdin.isatty", return_value=True),
            mock.patch("builtins.input", side_effect=[""] * 10 + ["yes"]),
        ):
            self.assertEqual(add.run(args, self.ctx), 0)
        self.assertEqual(load_registry(self.paths.services_dir).services["demo"].data, (self.data,))

    def test_start_and_backup_are_explicit_and_failure_keeps_registration(self) -> None:
        args = self.args("--start", "--backup", "--data", str(self.data))
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(add, "report_ready", return_value=False),
            mock.patch.object(add.backup, "backup_one") as take_backup,
        ):
            self.assertEqual(add.run(args, self.ctx), 1)
        self.compose.pull.assert_called_once_with()
        self.compose.up.assert_called_once_with(pull=False)
        take_backup.assert_not_called()
        self.assertIn("demo", load_registry(self.paths.services_dir).services)

    def test_start_health_and_first_backup(self) -> None:
        args = self.args(
            "--start", "--backup", "--data", str(self.data), "--url", "https://tasks.example/focus/"
        )
        with (
            mock.patch.object(self.ctx.shell, "ensure_sudo"),
            mock.patch.object(add, "report_ready", return_value=True),
            mock.patch.object(add.health, "run", return_value=0) as verify,
            mock.patch.object(add.backup, "backup_one") as take_backup,
        ):
            self.assertEqual(add.run(args, self.ctx), 0)
        verify.assert_called_once()
        take_backup.assert_called_once()
        self.assertIn("preserve existing handlers", self.out.getvalue())
        self.assertIn("does not install or verify", self.out.getvalue())


class OnboardingValidationTest(TempDirTest):
    def test_checks_image_pins_component_and_migrations(self) -> None:
        for raw, model in (
            ({}, {"services": {"app": {"image": "app:latest"}}}),
            ({"update_component": "db"}, {"services": {"app": {"image": "app:1"}}}),
            ({"migrate": "missing"}, {"services": {"app": {"image": "app:1"}}}),
            ({}, {"services": {"app": {"image": "app:1", "network_mode": "host"}}}),
        ):
            with self.subTest(raw=raw, model=model), self.assertRaises(SrvrError):
                validate_model(self.service(**raw), model)

    def test_reports_storage_not_covered_by_backups(self) -> None:
        model = {
            "services": {
                "app": {
                    "image": "app:1",
                    "volumes": [
                        {"type": "bind", "source": "/srv/app"},
                        {"type": "volume", "source": "db"},
                    ],
                }
            }
        }
        warnings = validate_model(self.service(), model)
        self.assertEqual(len(warnings), 2)
        self.assertFalse(
            validate_model(
                self.service(data=["/srv/app"]),
                {
                    "services": {
                        "app": {
                            "image": "app:1",
                            "volumes": [{"type": "bind", "source": "/srv/app/sub"}],
                        }
                    }
                },
            )
        )

    def test_atomic_save_refuses_overwrite_and_round_trips_toml(self) -> None:
        raw = {
            "kind": "image",
            "workdir": str(self.tmp / "work"),
            "description": 'A "quoted" café\nnext line',
        }
        service = self.service(description=raw["description"])
        file = save_registration(self.paths, service, raw)
        self.assertEqual(
            load_registry(self.paths.services_dir).services["demo"].description, raw["description"]
        )
        with self.assertRaisesRegex(SrvrError, "already registered"):
            save_registration(self.paths, service, {**raw, "description": "changed"})
        self.assertIn("quoted", file.read_text())
        self.assertEqual(list(self.paths.services_dir.glob(".add-*")), [])
