"""Read-only deploy checkouts: fetch, resolve, compare and move between commits."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .errors import SrvrError
from .shell import Shell
from .ui import iso, utc_now


class Checkout:
    def __init__(self, path: Path, shell: Shell) -> None:
        self.path = path
        self.shell = shell

    def _git(self, *args: str, check: bool = True) -> str:
        return self.shell.run(["git", "-C", str(self.path), *args], check=check).stdout.strip()

    def _ok(self, *args: str) -> bool:
        return self.shell.run(["git", "-C", str(self.path), *args], check=False).ok

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def branch(self) -> str | None:
        """Checked-out branch, or None when HEAD is detached."""
        name = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return name or None

    def is_clean(self) -> bool:
        return self._git("status", "--porcelain") == ""

    def fetch(self, branch: str) -> bool:
        """Fetch one branch into origin/<branch>, independent of the configured refspec.

        Returns False when the fetch fails (for example, no such branch)."""
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        return self._ok("fetch", "--quiet", "--tags", "origin", refspec)

    def resolve(self, ref: str) -> str:
        """Commit for a branch (preferring origin/<ref>), tag or commit ID."""
        for candidate in (f"refs/remotes/origin/{ref}", ref):
            sha = self._git(
                "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", check=False
            )
            if sha:
                return sha
        raise SrvrError(f"unknown ref {ref!r} in {self.path}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._ok("merge-base", "--is-ancestor", ancestor, descendant)

    def log(self, start: str, end: str, *, limit: int = 30) -> list[str]:
        out = self._git(
            "log", "--oneline", "--no-decorate", f"--max-count={limit}", f"{start}..{end}"
        )
        return out.splitlines()

    def describe(self, rev: str = "HEAD") -> str:
        return self._git("describe", "--tags", "--always", rev)

    def build_stamp(self) -> dict[str, str]:
        """Build variables naming the checked-out commit, read by apps that show their version."""
        committed = int(self._git("show", "--no-patch", "--format=%ct", "HEAD"))
        return {
            "APP_REVISION": self.head(),
            "APP_COMMIT_TIME": iso(datetime.fromtimestamp(committed, UTC)),
            "APP_BUILD_TIME": iso(utc_now()),
        }

    def short(self, rev: str) -> str:
        return self._git("rev-parse", "--short", rev)

    def move_to(self, sha: str) -> None:
        """Point the working tree at `sha`, keeping a branch checkout on its branch."""
        if not self.is_clean():
            raise SrvrError(f"{self.path} has local changes; commit or discard them first")
        if self.branch():
            self._git("reset", "--hard", "--quiet", sha)
        else:
            self._git("checkout", "--quiet", "--detach", sha)
