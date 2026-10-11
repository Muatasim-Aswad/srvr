import contextlib
import io
import os
import unittest
from unittest import mock

from srvr import cli


class CliTest(unittest.TestCase):
    def test_every_command_module_is_discovered(self) -> None:
        modules = cli.discover()
        expected = {
            "add",
            "backup",
            "backups",
            "compose",
            "config",
            "doctor",
            "health",
            "history",
            "info",
            "logs",
            "prune",
            "restart",
            "restore",
            "rollback",
            "snapshot",
            "start",
            "status",
            "stop",
            "update",
        }
        self.assertEqual(set(modules), expected)
        for name, module in modules.items():
            with self.subTest(name):
                self.assertTrue(module.HELP and module.HELP[0].isupper())

    def test_every_command_has_help(self) -> None:
        parser = cli.build_parser(cli.discover())
        for name in cli.discover():
            with self.subTest(name), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as exit_:
                    parser.parse_args([name, "--help"])
                self.assertEqual(exit_.exception.code, 0)
                self.assertIn(f"srvr {name}", out.getvalue())

    def test_no_command_means_status(self) -> None:
        with mock.patch.object(cli.discover()["status"], "run", return_value=0) as run:
            self.assertEqual(cli.main([]), 0)
            self.assertEqual(run.call_args.args[0].command, "status")

    def test_expected_errors_print_without_traceback(self) -> None:
        empty = {"XDG_CONFIG_HOME": "/nonexistent-srvr-test"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, empty), contextlib.redirect_stderr(err):
            code = cli.main(["info", "vocab"])
        self.assertEqual(code, 1)
        self.assertIn("no service registry", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_stop_requires_names_or_all(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["stop"]), 1)
        self.assertIn("--all", err.getvalue())
