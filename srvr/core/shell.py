"""Subprocess wrapper: every external command srvr runs goes through `Shell`."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import SrvrError

Argv = Sequence[str | Path]


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


class Shell:
    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose

    @staticmethod
    def _command(argv: Argv, sudo: bool) -> list[str]:
        """As root (the nightly backup), run privileged commands without sudo's log noise."""
        elevate = sudo and os.geteuid() != 0
        return (["sudo", "--"] if elevate else []) + [str(arg) for arg in argv]

    def _trace(self, text: str) -> None:
        if self.verbose:
            print(f"$ {text}", file=sys.stderr)

    def run(
        self,
        argv: Argv,
        *,
        sudo: bool = False,
        capture: bool = True,
        check: bool = True,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Result:
        """Run a command. `capture=False` streams output to the terminal."""
        command = self._command(argv, sudo)
        self._trace(shlex.join(command))
        try:
            proc = subprocess.run(
                command, cwd=cwd, text=True, capture_output=capture, timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            raise SrvrError(f"command not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SrvrError(f"timed out after {timeout}s: {shlex.join(command)}") from exc
        result = Result(proc.returncode, proc.stdout or "", proc.stderr or "")
        if check and not result.ok:
            detail = _last_line(result.stderr) or f"exit status {result.returncode}"
            raise SrvrError(f"`{shlex.join(command)}` failed: {detail}")
        return result

    def pipeline(
        self, first: Argv, second: Argv, *, sudo_first: bool = False, sudo_second: bool = False
    ) -> None:
        """Run `first | second`; fail if either side fails."""
        a, b = self._command(first, sudo_first), self._command(second, sudo_second)
        self._trace(f"{shlex.join(a)} | {shlex.join(b)}")
        # stderr goes to temp files so a chatty producer cannot block on a full pipe
        with tempfile.TemporaryFile("w+") as err_a, tempfile.TemporaryFile("w+") as err_b:
            children: list[subprocess.Popen[bytes]] = []
            try:
                producer = subprocess.Popen(a, stdout=subprocess.PIPE, stderr=err_a)
                children.append(producer)
                assert producer.stdout is not None
                try:
                    consumer = subprocess.Popen(b, stdin=producer.stdout, stderr=err_b)
                    children.append(consumer)
                finally:
                    producer.stdout.close()
                consumer_rc = consumer.wait()
                if consumer_rc != 0:
                    err_b.seek(0)
                    detail = _last_line(err_b.read()) or f"exit status {consumer_rc}"
                    raise SrvrError(f"`{shlex.join(b)}` failed: {detail}")
                producer_rc = producer.wait()
                for rc, command, err in ((consumer_rc, b, err_b), (producer_rc, a, err_a)):
                    if rc != 0:
                        err.seek(0)
                        detail = _last_line(err.read()) or f"exit status {rc}"
                        raise SrvrError(f"`{shlex.join(command)}` failed: {detail}")
            except FileNotFoundError as exc:
                raise SrvrError(f"command not found: {exc.filename}") from exc
            finally:
                for child in reversed(children):
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                    child.wait()

    def has_sudo(self) -> bool:
        """True when sudo works without a password prompt right now."""
        return self.run(["sudo", "-n", "true"], check=False).ok

    def ensure_sudo(self) -> None:
        """Prompt for the sudo password once so later sudo calls reuse the cached credential."""
        if self.has_sudo():
            return
        if not sys.stdin.isatty():
            raise SrvrError("sudo needs a password; run srvr from an interactive terminal")
        if not self.run(["sudo", "-v"], capture=False, check=False).ok:
            raise SrvrError("sudo authentication failed")
