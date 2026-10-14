"""`srvr compose` argument handling and the `srvr info --markdown` index."""

from unittest import mock

from srvr import cli
from srvr.commands.compose import refuse_destructive
from srvr.commands.info import markdown
from srvr.core.compose import Compose
from srvr.core.errors import SrvrError
from srvr.core.shell import Shell
from tests.helpers import TempDirTest


class ComposeCommandTest(TempDirTest):
    def test_everything_after_the_service_goes_to_compose(self) -> None:
        parser = cli.build_parser(cli.discover())
        args = parser.parse_args(["compose", "vocab", "logs", "-f", "--tail", "5", "api"])
        self.assertEqual(
            (args.service, args.compose_args), ("vocab", ["logs", "-f", "--tail", "5", "api"])
        )

    def test_refuses_down_with_volumes(self) -> None:
        for bad in (["down", "-v"], ["down", "--volumes"]):
            with self.subTest(bad), self.assertRaises(SrvrError):
                refuse_destructive(bad)
        refuse_destructive(["down"])
        refuse_destructive(["logs", "-v"])  # -v only matters for down


class ComposeBuildTest(TempDirTest):
    def test_build_passes_the_stamp_through_sudo_with_env(self) -> None:
        shell = mock.Mock()
        shell.run.return_value.stdout = '{"services": {"app": {"image": "app:1"}}}'
        compose = Compose(self.service("demo"), shell)
        compose.build({"APP_REVISION": "abc", "APP_BUILD_TIME": "2026-10-01T12:00:00Z"})
        argv = shell.run.call_args.args[0]
        self.assertEqual(
            argv[:3], ["env", "APP_REVISION=abc", "APP_BUILD_TIME=2026-10-01T12:00:00Z"]
        )
        self.assertEqual(argv[3:7], ["env", "COMPOSE_PROFILES=", "docker", "compose"])
        self.assertEqual(argv[-1], "build")

    def test_build_without_a_stamp_is_plain_compose(self) -> None:
        shell = mock.Mock()
        shell.run.return_value.stdout = '{"services": {"app": {"image": "app:1"}}}'
        Compose(self.service("demo"), shell).build()
        self.assertEqual(
            shell.run.call_args.args[0][:4], ["env", "COMPOSE_PROFILES=", "docker", "compose"]
        )


class MarkdownIndexTest(TempDirTest):
    def test_index_and_emergency_commands(self) -> None:
        git = self.service(
            "vocab",
            kind="git",
            branch="main",
            env_file="/etc/vocab.env",
            url="https://v.example",
            health="http://127.0.0.1:3000/health",
            data=["/var/lib/vocab"],
        )
        image = self.service("memos")
        image.workdir.mkdir()
        image.compose_files[0].write_text("services:\n  memos:\n    image: neosmemo/memos:0.30.0\n")
        ctx = mock.Mock()
        ctx.paths = self.paths
        ctx.compose.side_effect = lambda s: (
            Compose(s, Shell())
            if s.is_git
            else mock.Mock(
                image_refs=lambda: {"memos": "neosmemo/memos:0.30.0"},
                argv=Compose(s, Shell()).argv,
            )
        )
        text = markdown(ctx, [git, image])
        self.assertIn(
            "| vocab | git | `https://v.example` | `http://127.0.0.1:3000/health` |", text
        )
        self.assertIn("| memos | image | - | - | `neosmemo/memos:0.30.0` | none |", text)
        self.assertIn(
            "sudo env COMPOSE_PROFILES= docker compose --project-name vocab --project-directory",
            text,
        )
        self.assertIn("--env-file /etc/vocab.env", text)
        self.assertTrue(text.rstrip().endswith("```"))
