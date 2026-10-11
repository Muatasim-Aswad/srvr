import os
from unittest import mock

from srvr.core import snapshots
from srvr.core.errors import SrvrError
from tests.helpers import NoSudoShell, TempDirTest


class SnapshotsTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.tmp / "live" / "data"
        (self.data / "sub").mkdir(parents=True)
        (self.data / "db.sqlite").write_text("v1")
        (self.data / "sub" / "file").write_text("nested")
        os.chmod(self.data / "db.sqlite", 0o600)
        self.svc = self.service(data=[str(self.data)])
        self.dest = self.tmp / "staging"
        self.shell = NoSudoShell()

    def mirrored(self, name: str) -> str:
        copy = snapshots.location(self.dest, self.svc) / self.data.relative_to("/")
        return (copy / name).read_text()

    def test_copy_mirrors_data_privately_and_replaces_the_previous_copy(self) -> None:
        snapshots.copy(self.shell, self.svc, self.dest)
        self.assertEqual(self.mirrored("sub/file"), "nested")
        copy = snapshots.location(self.dest, self.svc)
        for private in (self.dest, copy):
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        db = copy / self.data.relative_to("/") / "db.sqlite"
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)

        (self.data / "db.sqlite").write_text("v2")
        (self.data / "sub" / "file").unlink()
        snapshots.copy(self.shell, self.svc, self.dest)
        self.assertEqual(self.mirrored("db.sqlite"), "v2")
        self.assertFalse((db.parent / "sub" / "file").exists())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["demo"])

    def test_failed_copy_keeps_the_previous_one(self) -> None:
        snapshots.copy(self.shell, self.svc, self.dest)
        (self.data / "db.sqlite").write_text("v2")
        real_run = self.shell.run

        def failing_cp(argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[0] == "cp":
                raise SrvrError("disk full")
            return real_run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=failing_cp),
            self.assertRaisesRegex(SrvrError, "disk full"),
        ):
            snapshots.copy(self.shell, self.svc, self.dest)
        self.assertEqual(self.mirrored("db.sqlite"), "v1")
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["demo"])

    def test_errors(self) -> None:
        with self.assertRaisesRegex(SrvrError, "no data"):
            snapshots.copy(self.shell, self.service(), self.dest)
        gone = self.service(data=[str(self.tmp / "gone")])
        with self.assertRaisesRegex(SrvrError, "missing"):
            snapshots.copy(self.shell, gone, self.dest)

    def test_failed_replacement_keeps_the_previous_snapshot(self) -> None:
        snapshots.copy(self.shell, self.svc, self.dest)
        (self.data / "db.sqlite").write_text("v2")
        run = self.shell.run

        def fail_publish(argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[0] == "mv" and str(argv[-2]) == str(self.dest / ".demo.partial"):
                raise SrvrError("cannot publish snapshot")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_publish),
            self.assertRaisesRegex(SrvrError, "cannot publish snapshot"),
        ):
            snapshots.copy(self.shell, self.svc, self.dest)
        self.assertEqual(self.mirrored("db.sqlite"), "v1")
        self.assertEqual((self.data / "db.sqlite").read_text(), "v2")

    def test_retry_recovers_an_interrupted_swap_before_a_copy_failure(self) -> None:
        snapshots.copy(self.shell, self.svc, self.dest)
        final = snapshots.location(self.dest, self.svc)
        final.rename(self.dest / ".demo.previous")
        (self.data / "db.sqlite").write_text("v2")
        run = self.shell.run

        def fail_copy(argv, **kwargs):
            if argv[0] == "cp":
                raise SrvrError("disk full")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_copy),
            self.assertRaisesRegex(SrvrError, "disk full"),
        ):
            snapshots.copy(self.shell, self.svc, self.dest)
        self.assertEqual(self.mirrored("db.sqlite"), "v1")
