"""History log, deploy records, settings and locking."""

from pathlib import Path
from unittest import mock

from srvr.core import deploys, history
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.settings import Settings
from tests.helpers import TempDirTest


class StateTest(TempDirTest):
    def test_history_newest_first_and_skips_torn_lines(self) -> None:
        history.record(self.paths, "demo", "backup", "ok", backup="b1")
        history.record(self.paths, "demo", "update", "failed", before="a", after="b")
        with (self.paths.service_state("demo") / "history.jsonl").open("a") as log:
            log.write('{"torn": \n')
        events = history.read(self.paths, "demo")
        self.assertEqual([e.action for e in events], ["update", "backup"])
        self.assertEqual(history.read(self.paths, "demo", limit=1)[0].result, "failed")
        self.assertEqual(history.read(self.paths, "other"), [])

    def test_deploy_record_round_trip(self) -> None:
        before = deploys.Snapshot(
            commit="a" * 40, images={"api": "sha256:1"}, image_refs={"api": "x:1"}
        )
        record = deploys.DeployRecord("demo", "2026-09-29T12:00:00Z", "ok", before, backup="b1")
        deploys.save_record(self.paths, record)
        loaded = deploys.load_record(self.paths, "demo")
        self.assertEqual(loaded, record)
        assert loaded is not None
        self.assertEqual(loaded.before.version(), "aaaaaaaaa")
        self.assertIsNone(deploys.load_record(self.paths, "other"))

    def test_snapshot_version_for_images(self) -> None:
        snapshot = deploys.Snapshot(image_refs={"a": "memos:0.30.0", "b": "memos:0.30.0"})
        self.assertEqual(snapshot.version(), "memos:0.30.0")

    def test_settings(self) -> None:
        self.assertEqual(Settings.load(self.tmp / "missing.toml"), Settings())
        file = self.tmp / "srvr.toml"
        file.write_text("keep_backups = 3\n")
        self.assertEqual(Settings.load(file).keep_backups, 3)
        for bad in ("keep_backups = 0\n", "keep_backups = true\n", "colour = 1\n"):
            file.write_text(bad)
            with self.subTest(bad), self.assertRaises(SrvrError):
                Settings.load(file)

    def test_lock_is_exclusive(self) -> None:
        with (
            service_lock(self.paths, "demo"),
            self.assertRaisesRegex(SrvrError, "another srvr command"),
            service_lock(self.paths, "demo"),
        ):
            pass
        with service_lock(self.paths, "demo"):
            pass  # released after the first block

    def test_root_runs_leave_new_state_to_the_home_owner(self) -> None:
        (self.tmp / "state").mkdir()
        home = self.tmp.stat()
        with (
            mock.patch("os.geteuid", return_value=0),
            mock.patch("os.chown") as chown,
            mock.patch.object(Path, "home", return_value=self.tmp),
            service_lock(self.paths, "demo"),
        ):
            pass
        state = self.paths.service_state("demo")
        self.assertEqual(
            [call.args for call in chown.call_args_list],
            [(state, home.st_uid, home.st_gid), (state / "lock", home.st_uid, home.st_gid)],
        )
