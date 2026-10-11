import io
import unittest
from datetime import UTC, datetime

from srvr.core.ui import UI, ago, human_size, render_table


class UITest(unittest.TestCase):
    def test_table_ignores_color_codes_when_aligning(self) -> None:
        table = render_table(["A", "B"], [["\x1b[32mok\x1b[0m", "x"], ["long", "y"]])
        lines = table.splitlines()
        self.assertEqual(lines[0], "A     B")
        self.assertEqual(lines[1], "\x1b[32mok\x1b[0m    x")

    def test_plain_output_when_not_a_tty(self) -> None:
        out = io.StringIO()
        UI(out=out, err=out).ok("done")
        self.assertEqual(out.getvalue(), "✓ done\n")

    def test_section_is_a_blank_line_and_a_titled_rule(self) -> None:
        out = io.StringIO()
        UI(out=out, err=out).section("vocab")
        blank, rule = out.getvalue().splitlines()
        self.assertEqual(blank, "")
        self.assertTrue(rule.startswith("── vocab ───"))
        self.assertLessEqual(len(rule), 80)

    def test_ago_and_size(self) -> None:
        now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
        self.assertEqual(ago(datetime(2026, 9, 27, 11, 0, tzinfo=UTC), now), "2d ago")
        self.assertEqual(ago(now, now), "just now")
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(7_122_944), "6.8 MiB")
