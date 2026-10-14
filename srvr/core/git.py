"""Read-only deploy checkouts: fetch, resolve, compare and move between commits."""

from __future__ import annotations

import re
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

    def fetch_ref(self, ref: str) -> str | None:
        """Refresh an explicit branch/tag/commit without requiring the registered branch."""
        branch = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
        tag = ref if ref.startswith("refs/tags/") else f"refs/tags/{ref}"
        result = self.shell.run(
            ["git", "-C", str(self.path), "ls-remote", "--refs", "origin", branch, tag],
            check=False,
        )
        if not result.ok:
            return None
        remote = {line.split("\t", 1)[1] for line in result.stdout.splitlines() if "\t" in line}
        if branch in remote:
            name = branch.removeprefix("refs/heads/")
            return f"refs/remotes/origin/{name}" if self.fetch(name) else None
        if tag in remote:
            return tag if self._ok("fetch", "--quiet", "--tags", "origin", f"{tag}:{tag}") else None
        local_tag = self._git("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}", check=False)
        if not local_tag and not re.fullmatch(r"[0-9a-fA-F]{4,40}", ref):
            return None
        if not self._ok("fetch", "--quiet", "--tags", "origin", "refs/tags/*:refs/tags/*"):
            return None
        if local_tag:
            return tag
        commit = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if commit:
            return commit
        if len(ref) == 40 and self._ok("fetch", "--quiet", "--no-tags", "origin", ref):
            return (
                self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
                or None
            )
        return None

    def resolve(self, ref: str) -> str:
        """Commit for a branch (preferring origin/<ref>), tag or commit ID."""
        candidates = (
            (ref,)
            if ref.startswith("refs/") or re.fullmatch(r"[0-9a-fA-F]{40}", ref)
            else (f"refs/remotes/origin/{ref}", ref)
        )
        for candidate in candidates:
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
