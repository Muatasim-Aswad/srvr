from argparse import Namespace
from unittest import mock

from srvr.commands.update import Plan, plan_image, update_order
from srvr.core.errors import SrvrError
from tests.helpers import TempDirTest


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
