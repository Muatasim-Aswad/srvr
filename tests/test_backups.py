import json
import os
import subprocess
import sys
from unittest import mock

from srvr.core import backups, deploys
from srvr.core.errors import SrvrError
from tests.helpers import NoSudoShell, TempDirTest


class BackupsTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.tmp / "live" / "data"
        (self.data / "sub").mkdir(parents=True)
        (self.data / "db.sqlite").write_text("v1")
        (self.data / "sub" / "file").write_text("nested")
        os.chmod(self.data / "db.sqlite", 0o600)
        self.svc = self.service(data=[str(self.data)])
        self.shell = NoSudoShell()

    def test_create_restore_round_trip(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        self.assertEqual(taken.meta["version"], "abc")
        self.assertEqual(taken.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backups.find(self.paths, "demo", "latest").name, taken.name)

        (self.data / "db.sqlite").write_text("v2")
        kept = backups.restore(self.shell, self.svc, taken)

        self.assertEqual((self.data / "db.sqlite").read_text(), "v1")
        self.assertEqual((self.data / "sub" / "file").read_text(), "nested")
        self.assertEqual((self.data / "db.sqlite").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0] / "db.sqlite").read_text(), "v2")  # replaced data kept aside

    def test_restore_refuses_a_damaged_archive(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        with taken.path.open("ab") as archive:
            archive.write(b"garbage")
        with self.assertRaisesRegex(SrvrError, "checksum"):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.data / "db.sqlite").read_text(), "v1")

    def test_restore_refuses_other_data_paths(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        other = self.service(data=[str(self.tmp / "elsewhere")])
        with self.assertRaisesRegex(SrvrError, "differ"):
            backups.restore(self.shell, other, taken)

    def test_restore_keeps_all_live_data_when_moving_a_later_directory_fails(self) -> None:
        uploads = self.tmp / "live" / "uploads"
        uploads.mkdir()
        (uploads / "file").write_text("old upload")
        service = self.service(data=[str(self.data), str(uploads)])
        taken = backups.create(self.shell, self.paths, service, reason="manual", version="abc")
        (self.data / "db.sqlite").write_text("current database")
        (uploads / "file").write_text("current upload")
        run = self.shell.run

        def fail_move(argv, **kwargs):  # type: ignore[no-untyped-def]
            if argv[0] == "mv" and str(argv[-2]) == str(uploads):
                raise SrvrError("cannot move uploads")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_move),
            self.assertRaisesRegex(SrvrError, "cannot move uploads"),
        ):
            backups.restore(self.shell, service, taken)
        self.assertEqual((self.data / "db.sqlite").read_text(), "current database")
        self.assertEqual((uploads / "file").read_text(), "current upload")

    def test_restore_extracts_before_touching_live_data(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        (self.data / "db.sqlite").write_text("current database")
        observed = []

        def fail_extract(*args, **kwargs):  # type: ignore[no-untyped-def]
            observed.append(self.data.exists())
            raise SrvrError("cannot extract archive")

        with (
            mock.patch.object(self.shell, "pipeline", side_effect=fail_extract),
            self.assertRaisesRegex(SrvrError, "cannot extract archive"),
        ):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual(observed, [True])
        self.assertEqual((self.data / "db.sqlite").read_text(), "current database")

    def test_retry_recovers_live_data_after_a_process_dies_during_restore(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        (self.data / "db.sqlite").write_text("current database")
        service_json = json.dumps(
            {"kind": "image", "workdir": str(self.svc.workdir), "data": [str(self.data)]}
        )
        code = f"""
import json, os
from pathlib import Path
from srvr.core import backups
from srvr.core.registry import parse_service
from tests.helpers import NoSudoShell
class CrashShell(NoSudoShell):
    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if argv[0] == "mv" and str(argv[-2]) == {str(self.data)!r}:
            os._exit(70)
        return result
service = parse_service("demo", json.loads({service_json!r}), Path("unused"))
archive = backups.Backup(Path({str(taken.path)!r}), json.loads({json.dumps(taken.meta)!r}))
backups.restore(CrashShell(), service, archive)
"""
        result = subprocess.run(
            [sys.executable, "-c", code], timeout=10, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 70, result.stderr)
        with taken.path.open("ab") as archive:
            archive.write(b"damaged")
        with self.assertRaisesRegex(SrvrError, "checksum"):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.data / "db.sqlite").read_text(), "current database")

    def test_prune_keeps_newest(self) -> None:
        directory = self.paths.service_backups("demo")
        directory.mkdir(parents=True)
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            (directory / f"{stamp}-manual.tar.zst").write_bytes(b"")
            (directory / f"{stamp}-manual.json").write_text("{}")
        removed = backups.prune(self.paths, "demo", keep=2)
        self.assertEqual([b.name for b in removed], ["20260101T000000Z-manual"])
        self.assertEqual(
            [b.name for b in backups.list_backups(self.paths, "demo")],
            ["20260103T000000Z-manual", "20260102T000000Z-manual"],
        )
        self.assertFalse((directory / "20260101T000000Z-manual.json").exists())

    def test_backups_in_the_same_second_do_not_overwrite_each_other(self) -> None:
        moment = backups.utc_now()
        with mock.patch("srvr.core.backups.utc_now", return_value=moment):
            first = backups.create(self.shell, self.paths, self.svc, reason="manual", version="one")
            (self.data / "db.sqlite").write_text("v2")
            second = backups.create(
                self.shell, self.paths, self.svc, reason="manual", version="two"
            )
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(backups.find(self.paths, "demo", "latest").name, second.name)
        backups.restore(self.shell, self.svc, first)
        self.assertEqual((self.data / "db.sqlite").read_text(), "v1")

    def test_errors(self) -> None:
        with self.assertRaisesRegex(SrvrError, "no backups"):
            backups.find(self.paths, "demo", "latest")
        with self.assertRaisesRegex(SrvrError, "no data"):
            backups.create(self.shell, self.paths, self.service(), reason="x", version="y")

    def test_interrupt_after_installing_data_recovers_the_original(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        (self.data / "db.sqlite").write_text("current")
        run = self.shell.run

        def interrupt(argv, **kwargs):
            result = run(argv, **kwargs)
            if (
                argv[0] == "mv"
                and str(argv[-1]) == str(self.data)
                and ".srvr-restore-" in str(argv[-2])
            ):
                raise KeyboardInterrupt
            return result

        with (
            mock.patch.object(self.shell, "run", side_effect=interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.data / "db.sqlite").read_text(), "current")
        self.assertFalse((taken.path.parent / ".restore.json").exists())

    def test_failed_recovery_retains_journal_and_can_be_retried(self) -> None:
        taken = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        (self.data / "db.sqlite").write_text("current")
        run = self.shell.run

        def fail_switch_and_repair(argv, **kwargs):
            if argv[0] == "mv" and str(argv[-1]) == str(self.data):
                raise SrvrError("destination unavailable")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_switch_and_repair),
            self.assertRaisesRegex(SrvrError, "recovery incomplete"),
        ):
            backups.restore(self.shell, self.svc, taken)
        self.assertTrue((taken.path.parent / ".restore.json").exists())
        with taken.path.open("ab") as archive:
            archive.write(b"damaged")
        with self.assertRaisesRegex(SrvrError, "checksum"):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.data / "db.sqlite").read_text(), "current")
        self.assertFalse((taken.path.parent / ".restore.json").exists())

    def test_retention_preserves_the_backup_referenced_by_rollback(self) -> None:
        first = backups.create(self.shell, self.paths, self.svc, reason="update", version="abc")
        record = deploys.DeployRecord("demo", "now", "ok", deploys.Snapshot(), backup=first.name)
        deploys.save_record(self.paths, record)
        second = backups.create(self.shell, self.paths, self.svc, reason="manual", version="abc")
        backups.prune(self.paths, "demo", keep=1)
        self.assertEqual(
            {b.name for b in backups.list_backups(self.paths, "demo")}, {first.name, second.name}
        )
