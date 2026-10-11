import unittest

from srvr.core.composefile import find_image, set_image, validate_image_ref
from srvr.core.errors import SrvrError

COMPOSE = """\
name: memos
services:
  memos:
    # pinned release
    image: neosmemo/memos:0.30.0  # keep pinned
    ports:
      - "127.0.0.1:5230:5230"
    labels:
      image: not-this-one
  sidecar:
    image: "busybox:1.36"
volumes:
  image: {}
"""


class ComposeFileTest(unittest.TestCase):
    def test_find_image(self) -> None:
        self.assertEqual(find_image(COMPOSE, "memos"), (4, "neosmemo/memos:0.30.0"))
        self.assertEqual(find_image(COMPOSE, "sidecar")[1], "busybox:1.36")

    def test_set_image_changes_only_that_value(self) -> None:
        updated = set_image(COMPOSE, "memos", "neosmemo/memos:0.31.0")
        self.assertIn("    image: neosmemo/memos:0.31.0  # keep pinned\n", updated)
        self.assertEqual(updated.replace("0.31.0", "0.30.0"), COMPOSE)

    def test_set_image_keeps_quotes(self) -> None:
        updated = set_image(COMPOSE, "sidecar", "busybox:1.37")
        self.assertIn('    image: "busybox:1.37"\n', updated)

    def test_missing_service(self) -> None:
        with self.assertRaises(SrvrError):
            find_image(COMPOSE, "absent")

    def test_validate_image_ref(self) -> None:
        digest = "sha256:" + "a" * 64
        for ok in ("memos:0.30.0", f"memos@{digest}", f"ghcr.io/x/y:1.2@{digest}", "reg:5000/y:2"):
            with self.subTest(ok):
                validate_image_ref(ok)
        for bad in ("memos", "memos:latest", "reg:5000/y", "memos@sha256:short", "a b:1", ""):
            with self.subTest(bad), self.assertRaises(SrvrError):
                validate_image_ref(bad)
