import io
from argparse import ArgumentParser, Namespace
from unittest import mock

from srvr.commands import update
from srvr.commands.update import Plan, plan_image, update_order
from srvr.core import deploys
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.onboarding import save_registration
from srvr.core.registry import parse_service
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest
from tests.test_git import git


class UpdateOrderTest(TempDirTest):
    def test_git_services_come_first_and_keep_their_order(self) -> None:
        services = [
            self.service("memos"),
            self.service("jotter", kind="git", branch="main"),
            self.service("tududi"),
            self.service("vocab", kind="git", branch="main"),
        ]
        self.assertEqual(
            [s.name for s in update_order(services)], ["jotter", "vocab", "memos", "tududi"]
        )


class PlanImageTest(TempDirTest):
    COMPOSE = "services:\n  memos:\n    image: neosmemo/memos:0.30.0\n"

    def plan(self, *, named: bool, image: str | None = None) -> tuple[Plan | None, mock.Mock]:
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        (work / "compose.yaml").write_text(self.COMPOSE)
        ctx = mock.Mock()
        ctx.paths = self.paths
        ctx.compose.return_value.config.return_value = {
            "services": {"memos": {"image": "neosmemo/memos:0.30.0"}}
        }
        args = Namespace(image=image, check=False, force=False)
        return plan_image(ctx, self.service("memos"), args, named=named), ctx

    def test_unnamed_image_service_without_image_is_skipped(self) -> None:
        plan, ctx = self.plan(named=False)
        self.assertIsNone(plan)
        self.assertIn("skipped", ctx.ui.line.call_args.args[0])

    def test_named_image_service_without_image_is_an_error(self) -> None:
        with self.assertRaisesRegex(SrvrError, "--image"):
            self.plan(named=True)

    def test_named_image_service_with_image_plans_the_pin(self) -> None:
        plan, _ = self.plan(named=True, image="neosmemo/memos:0.31.0")
        assert plan is not None
        self.assertEqual(
            (plan.current, plan.target), ("neosmemo/memos:0.30.0", "neosmemo/memos:0.31.0")
        )


class GitRefPlanningTest(TempDirTest):
    def setUp(self):
        super().setUp()
        seed = self.tmp / "seed"
        self.seed = seed
        seed.mkdir()
        git(seed, "init", "--quiet", "--initial-branch=dev")
        (seed / "compose.yaml").write_text("services:\n  app:\n    image: example/app:1\n")
        git(seed, "add", ".")
        git(seed, "commit", "--quiet", "-m", "Initial dev")
        self.first = git(seed, "rev-parse", "HEAD")
        git(seed, "branch", "main")
        self.remote = self.tmp / "remote.git"
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "--quiet", "--bare", str(seed), str(self.remote))
        git(self.tmp, "clone", "--quiet", str(self.remote), str(self.work))
        git(seed, "checkout", "--quiet", "main")
        (seed / "change").write_text("new main\n")
        git(seed, "add", ".")
        git(seed, "commit", "--quiet", "-m", "Advance main")
        self.target = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "--quiet", str(self.remote), "main")
        git(self.remote, "update-ref", "-d", "refs/heads/dev")
        raw = {"kind": "git", "branch": "dev", "workdir": str(self.work)}
        self.service_ = parse_service("example", raw, self.paths.services_dir / "example.toml")
        save_registration(self.paths, self.service_, raw)
        deploys.save_record(
            self.paths,
            deploys.DeployRecord(
                "example", "2026-10-14T00:00:00Z", "ok", deploys.Snapshot(commit=self.first)
            ),
        )
        self.backup = self.paths.service_backups("example") / "kept.tar.zst"
        self.backup.parent.mkdir(parents=True)
        self.backup.write_bytes(b"previous backup")
        self.out, self.err = io.StringIO(), io.StringIO()
        self.calls = []
        shell = NoSudoShell()
        run = shell.run

        def record(argv, **kwargs):
            self.calls.append(list(argv))
            return run(argv, **kwargs)

        shell.run = record
        self.ctx = Context(paths=self.paths, shell=shell, ui=UI(out=self.out, err=self.err))

    def args(self, *options):
        parser = ArgumentParser()
        update.configure(parser)
        return parser.parse_args(["example", "--check", *options])

    def unchanged(self):
        return (
            git(self.work, "rev-parse", "HEAD"),
            git(self.work, "status", "--porcelain"),
            (self.work / "compose.yaml").read_bytes(),
            self.service_.source.read_bytes(),
            (self.paths.service_state("example") / "last-deploy.json").read_bytes(),
            self.backup.read_bytes(),
        )

    def test_explicit_main_plans_after_registered_dev_is_deleted_without_changing_deployment(self):
        before = self.unchanged()
        self.assertEqual(update.run(self.args("--ref", "main"), self.ctx), 0)
        self.assertIn(self.ctx.checkout(self.service_).describe(self.target), self.out.getvalue())
        self.assertNotIn("fetching origin/dev", self.out.getvalue())
        self.assertEqual(self.unchanged(), before)
        self.assertFalse(any("docker" in call for call in self.calls))
        self.assertFalse((self.paths.service_state("example") / "images.compose.json").exists())

    def test_missing_default_still_fails_and_does_not_use_stale_origin_dev(self):
        before = self.unchanged()
        with self.assertRaisesRegex(SrvrError, "cannot fetch origin/dev"):
            update.run(self.args(), self.ctx)
        self.assertEqual(self.unchanged(), before)

    def test_fresh_remote_tag_and_full_branch_ref_need_no_registered_branch(self):
        git(self.work, "config", "remote.origin.fetch", "+refs/heads/dev:refs/remotes/origin/dev")
        git(self.work, "update-ref", "refs/remotes/origin/release-new", self.first)
        git(self.seed, "tag", "--annotate", "release-new", "--message", "Release", self.target)
        git(self.seed, "push", "--quiet", str(self.remote), "refs/tags/release-new")
        before = self.unchanged()
        for ref in ("release-new", "refs/tags/release-new", "refs/heads/main"):
            with self.subTest(ref=ref):
                self.assertEqual(update.run(self.args("--ref", ref), self.ctx), 0)
                self.assertEqual(self.unchanged(), before)
        self.assertEqual(git(self.work, "rev-parse", "refs/tags/release-new^{commit}"), self.target)
        self.assertIn("release-new", self.out.getvalue())

    def test_explicit_remote_commit_and_known_short_commit_and_local_tag_remain_supported(self):
        git(self.work, "config", "remote.origin.fetch", "+refs/heads/dev:refs/remotes/origin/dev")
        before = self.unchanged()
        git(self.work, "tag", "local-release", self.first)
        for ref in (self.target, self.first[:10], "local-release"):
            with self.subTest(ref=ref):
                self.assertEqual(update.run(self.args("--ref", ref, "--force"), self.ctx), 0)
                self.assertEqual(self.unchanged(), before)

    def test_fetch_failure_refuses_stale_origin_main_even_after_successful_remote_lookup(self):
        before = self.unchanged()
        run = self.ctx.shell.run

        def lose_remote(argv, **kwargs):
            if "fetch" in argv:
                self.remote.rename(self.tmp / "unavailable.git")
            return run(argv, **kwargs)

        self.ctx.shell.run = lose_remote
        with self.assertRaisesRegex(SrvrError, "cannot fetch origin/main"):
            update.run(self.args("--ref", "main"), self.ctx)
        self.assertEqual(self.unchanged(), before)
        self.assertNotIn("origin/dev", self.out.getvalue())

    def test_confirmation_revalidates_the_selected_target_without_changing_checkout(self):
        plan = update.plan_git(self.ctx, self.service_, self.args("--ref", "main"))
        self.assertIsNotNone(plan)
        plan.validate()
        git(self.work, "update-ref", "refs/remotes/origin/main", self.first)
        with self.assertRaisesRegex(SrvrError, "target ref changed"):
            plan.validate()
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.first)

    def test_default_requires_fast_forward_but_explicit_ref_allows_deliberate_divergence(self):
        self.service_.source.write_text(self.service_.source.read_text().replace('"dev"', '"main"'))
        (self.work / "local").write_text("deployed divergent commit")
        git(self.work, "add", ".")
        git(self.work, "commit", "--quiet", "-m", "Divergent deployment")
        before = self.unchanged()
        with self.assertRaisesRegex(SrvrError, "not ahead of the deployed commit"):
            update.run(self.args(), self.ctx)
        self.assertEqual(update.run(self.args("--ref", "main"), self.ctx), 0)
        self.assertIn("not ahead", self.err.getvalue())
        self.assertEqual(self.unchanged(), before)
