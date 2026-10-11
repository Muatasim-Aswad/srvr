"""State publication failures must preserve usable recovery records."""

from pathlib import Path
from unittest import mock

from srvr.core import deploys
from tests.helpers import TempDirTest


class DeployRecordTest(TempDirTest):
    def test_failed_record_publication_preserves_the_complete_previous_record(self) -> None:
        original = deploys.DeployRecord("demo", "first", "ok", deploys.Snapshot(), backup="old")
        deploys.save_record(self.paths, original)
        replacement = deploys.DeployRecord("demo", "second", "started", deploys.Snapshot())
        with (
            mock.patch.object(Path, "replace", side_effect=OSError("disk failure")),
            self.assertRaisesRegex(OSError, "disk failure"),
        ):
            deploys.save_record(self.paths, replacement)
        self.assertEqual(deploys.load_record(self.paths, "demo"), original)
        files = list(self.paths.service_state("demo").iterdir())
        self.assertEqual([file.name for file in files], ["last-deploy.json"])
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
