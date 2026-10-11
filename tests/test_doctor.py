"""`srvr doctor`: SSD and pending-reboot findings."""

import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from srvr.commands import doctor
from srvr.commands.doctor import FAIL, OK, WARN, assess_smart
from srvr.core import history
from srvr.core.settings import Settings
from srvr.core.ui import iso, parse_iso, utc_now
from tests.helpers import TempDirTest


def smart_report(**changes: object) -> dict:
    """A healthy drive, 9,782 power-on hours, self-tested two days ago."""
    report: dict = {
        "smart_status": {"passed": True},
        "power_on_time": {"hours": 9782},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}, "when_failed": ""},
                {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
                {"id": 198, "name": "Offline_Uncorrectable", "raw": {"value": 0}},
            ]
        },
        "ata_smart_error_log": {"summary": {"count": 0}},
        "ata_smart_self_test_log": {
            "standard": {
                "table": [
                    {
                        "lifetime_hours": 9734,
                        "status": {"string": "Completed without error", "passed": True},
                    }
                ]
            }
        },
    }
    report.update(changes)
    return report


def with_attribute(attribute_id: int, value: int, when_failed: str = "") -> dict:
    report = smart_report()
    for attribute in report["ata_smart_attributes"]["table"]:
        if attribute["id"] == attribute_id:
            attribute["raw"]["value"] = value
            attribute["when_failed"] = when_failed
    return report


class AssessSmartTest(unittest.TestCase):
    def levels(self, report: dict) -> list[tuple[str, str]]:
        return [(f.level, f.detail) for f in assess_smart("/dev/sda", report, Settings())]

    def test_healthy_drive_is_one_ok(self) -> None:
        self.assertEqual(
            self.levels(smart_report()), [(OK, "/dev/sda: healthy, 9782 power-on hours")]
        )

    def test_failed_health_check_fails(self) -> None:
        report = smart_report(smart_status={"passed": False})
        self.assertEqual(self.levels(report), [(FAIL, "/dev/sda: SMART health check failed")])

    def test_unreadable_sectors_fail_and_reallocated_ones_warn(self) -> None:
        self.assertEqual(
            self.levels(with_attribute(197, 2)), [(FAIL, "/dev/sda: 2 pending sector(s)")]
        )
        self.assertEqual(
            self.levels(with_attribute(5, 1)), [(WARN, "/dev/sda: 1 reallocated sector(s)")]
        )

    def test_attribute_below_threshold_fails(self) -> None:
        self.assertIn(
            (FAIL, "/dev/sda: below threshold: Reallocated_Sector_Ct"),
            self.levels(with_attribute(5, 0, when_failed="now")),
        )

    def test_drive_error_log_warns(self) -> None:
        report = smart_report(ata_smart_error_log={"summary": {"count": 3}})
        self.assertEqual(self.levels(report), [(WARN, "/dev/sda: 3 error(s) in the drive log")])

    def test_failed_self_test_fails(self) -> None:
        report = smart_report()
        report["ata_smart_self_test_log"]["standard"]["table"][0]["status"] = {
            "string": "Completed: read failure",
            "passed": False,
        }
        self.assertEqual(
            self.levels(report), [(FAIL, "/dev/sda: last self-test: Completed: read failure")]
        )

    def test_old_or_missing_self_test_warns(self) -> None:
        self.assertEqual(
            self.levels(smart_report(power_on_time={"hours": 9734 + 10 * 24})),
            [(WARN, "/dev/sda: last self-test 10 days ago")],
        )
        self.assertEqual(
            self.levels(smart_report(ata_smart_self_test_log={})),
            [(WARN, "/dev/sda: no self-test has run")],
        )

    def test_unreadable_drive_fails_with_smartctl_reason(self) -> None:
        report = {"smartctl": {"messages": [{"string": "Permission denied"}]}}
        self.assertEqual(self.levels(report), [(FAIL, "/dev/sda: Permission denied")])


class RebootTest(TempDirTest):
    def check(self, days_ago: float | None) -> tuple[str, str]:
        flag = self.tmp / "reboot-required"
        if days_ago is not None:
            flag.touch()
            then = time.time() - days_ago * 86400
            os.utime(flag, (then, then))
        ctx = SimpleNamespace(settings=Settings())
        with mock.patch.object(doctor, "REBOOT_FLAG", Path(flag)):
            [finding] = doctor.check_reboot(ctx)  # type: ignore[arg-type]
        return finding.level, finding.detail

    def test_no_flag_is_ok(self) -> None:
        self.assertEqual(self.check(None), (OK, "not required"))

    def test_recent_flag_warns_and_old_flag_fails(self) -> None:
        self.assertEqual(self.check(1.5)[0], WARN)
        self.assertEqual(
            self.check(3.1),
            (FAIL, "required for 3 day(s) by updates (needs someone on site)"),
        )


class BackupsTest(TempDirTest):
    def check(self) -> list[tuple[str, str]]:
        service = self.service(data=[str(self.tmp / "data-dir")])
        ctx = SimpleNamespace(
            paths=self.paths, settings=Settings(), services=lambda names: [service]
        )
        return [(f.level, f.detail) for f in doctor.check_backups(ctx)]  # type: ignore[arg-type]

    def snapshot_at(self, iso_time: str) -> None:
        with mock.patch("srvr.core.history.utc_now", return_value=parse_iso(iso_time)):
            history.record(self.paths, "demo", "snapshot", "ok")

    def test_service_without_copies_warns(self) -> None:
        self.assertEqual(self.check(), [(WARN, "demo: no backup or snapshot")])

    def test_recent_snapshot_counts_as_a_copy(self) -> None:
        self.snapshot_at(iso(utc_now()))
        self.assertEqual(self.check(), [(OK, "1 services copied within 7 days")])

    def test_failed_or_old_snapshot_does_not_count(self) -> None:
        history.record(self.paths, "demo", "snapshot", "failed")
        self.assertEqual(self.check(), [(WARN, "demo: no backup or snapshot")])
        self.snapshot_at("2020-01-01T00:00:00Z")
        self.assertEqual(self.check()[0][0], WARN)

    def test_unfinished_restore_is_reported_as_a_failure(self) -> None:
        directory = self.paths.service_backups("demo")
        directory.mkdir(parents=True)
        (directory / ".restore.json").write_text("{}")
        self.assertEqual(self.check()[0][0], FAIL)
        self.assertIn("unfinished restore", self.check()[0][1])
