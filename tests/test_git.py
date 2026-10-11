import subprocess
from datetime import UTC, datetime
from pathlib import Path

from srvr.core.errors import SrvrError
from srvr.core.git import Checkout
from srvr.core.shell import Shell
from srvr.core.ui import iso
from tests.helpers import TempDirTest


def git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
    }
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


class CheckoutTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "--quiet", "--initial-branch=main")
        self.first = self.commit("one")
        self.deploy = self.tmp / "deploy"
        git(self.tmp, "clone", "--quiet", str(self.upstream), str(self.deploy))
        self.checkout = Checkout(self.deploy, Shell())

    def commit(self, message: str) -> str:
        (self.upstream / "file").write_text(message)
        git(self.upstream, "add", "file")
        git(self.upstream, "commit", "--quiet", "-m", message)
        return git(self.upstream, "rev-parse", "HEAD")

    def test_fetch_resolve_and_fast_forward(self) -> None:
        second = self.commit("two")
        self.assertTrue(self.checkout.fetch("main"))
        target = self.checkout.resolve("main")
        self.assertEqual(target, second)
        self.assertTrue(self.checkout.is_ancestor(self.first, target))
        self.assertEqual(len(self.checkout.log(self.first, target)), 1)

        self.checkout.move_to(target)
        self.assertEqual(self.checkout.head(), second)
        self.assertEqual(self.checkout.branch(), "main")
        self.checkout.move_to(self.first)  # rollback keeps the branch checked out
        self.assertEqual((self.checkout.head(), self.checkout.branch()), (self.first, "main"))

    def test_detached_checkout_stays_detached(self) -> None:
        git(self.deploy, "checkout", "--quiet", "--detach")
        second = self.commit("two")
        self.checkout.fetch("main")
        self.checkout.move_to(second)
        self.assertEqual((self.checkout.head(), self.checkout.branch()), (second, None))

    def test_missing_branch_and_ref(self) -> None:
        self.assertFalse(self.checkout.fetch("nope"))
        with self.assertRaises(SrvrError):
            self.checkout.resolve("nope")

    def test_refuses_to_move_a_dirty_checkout(self) -> None:
        (self.deploy / "file").write_text("local edit")
        self.assertFalse(self.checkout.is_clean())
        with self.assertRaisesRegex(SrvrError, "local changes"):
            self.checkout.move_to(self.first)

    def test_build_stamp_names_the_checked_out_commit(self) -> None:
        stamp = self.checkout.build_stamp()
        self.assertEqual(stamp["APP_REVISION"], self.first)
        committed = int(git(self.deploy, "show", "--no-patch", "--format=%ct", "HEAD"))
        self.assertEqual(stamp["APP_COMMIT_TIME"], iso(datetime.fromtimestamp(committed, UTC)))
        self.assertRegex(stamp["APP_BUILD_TIME"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
