"""Test doubles and fixtures shared by the test modules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from srvr.core.paths import Paths
from srvr.core.registry import Service, parse_service
from srvr.core.shell import Shell


class NoSudoShell(Shell):
    """Runs commands for real but without sudo, so tests can use temp directories."""

    @staticmethod
    def _command(argv, sudo):  # type: ignore[no-untyped-def, override]
        return [str(arg) for arg in argv]


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.paths = Paths(
            config=self.tmp / "config", state=self.tmp / "state", data=self.tmp / "data"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def service(self, name: str = "demo", **raw: object) -> Service:
        spec: dict[str, object] = {"kind": "image", "workdir": str(self.tmp / "work")}
        spec.update(raw)
        return parse_service(name, spec, self.tmp / f"{name}.toml")
