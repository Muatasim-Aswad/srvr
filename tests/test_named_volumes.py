"""Named-volume recovery with real files/archives and a disposable Docker process boundary."""

import io
import json
import os
import socket
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from unittest import mock

from srvr.commands import backup, compose, restart, restore, snapshot, start
from srvr.core import backups, snapshots
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.lock import service_lock
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest

DOCKER = """
import json, sys
state = json.loads(open(sys.argv[1]).read())
args = sys.argv[2:]
if args[:2] == ["context", "inspect"]:
    print(json.dumps(state["endpoint"]))
elif args[0] == "info":
    print(json.dumps({"ID": state["daemon"], "DockerRootDir": state["root"]}))
elif args[:2] == ["volume", "inspect"]:
    name = args[-1]
    print(json.dumps([state["volumes"][name]]))
elif args[0] == "ps":
    if "label=com.docker.compose.oneoff=True" in args:
        pass
    elif "--quiet" in args:
        print("\\n".join(state["containers"]))
    else:
        for name, c in state["containers"].items():
            print("\\t".join([c["project"], "app", name,
                             "running" if c["running"] else "exited", "", name]))
elif args[0] == "inspect":
    print(json.dumps(state["containers"][args[-1]]))
elif args[0] == "compose":
    if "config" in args:
        print(json.dumps(state["model"]))
    else:
        action = next(a for a in args if a in ["stop", "start", "up"])
        state.setdefault("actions", []).append(action)
        for c in state["containers"].values():
            c["running"] = action != "stop"
        if not state["containers"] and action == "up":
            state["containers"]["fixture"] = {"project":"demo", "running":True, "paused":False}
        with open(sys.argv[1], "w") as handle:
            json.dump(state, handle)
else:
    raise SystemExit("unexpected Docker command")
"""


class VolumeShell(NoSudoShell):
    def __init__(self, executable: Path, state: Path) -> None:
        super().__init__()
        self.executable = executable
        self.state = state
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs):
        self.calls.append([str(a) for a in argv])
        if "docker" in argv:
            offset = list(argv).index("docker")
            argv = [sys.executable, self.executable, self.state, *argv[offset + 1 :]]
        return super().run(argv, **kwargs)

    def ensure_sudo(self) -> None:
        pass


class NamedVolumesTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "docker"
        self.live = self.root / "volumes" / "srvr-b2-fixture" / "_data"
        self.live.mkdir(parents=True)
        (self.live / "database").write_text("before")
        (self.live / ".hidden").write_text("hidden")
        (self.live / "link").symlink_to("database")
        self.live.chmod(0o700)
        (self.live / "database").chmod(0o600)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.tmp / "docker.sock"))
        self.addCleanup(self.sock.close)
        self.state = {
            "endpoint": f"unix://{self.tmp}/docker.sock",
            "daemon": "daemon-before",
            "root": str(self.root),
            "containers": {},
            "volumes": {
                "srvr-b2-fixture": {
                    "Name": "srvr-b2-fixture",
                    "Driver": "local",
                    "Scope": "local",
                    "Options": None,
                    "CreatedAt": "2026-10-01T00:00:00Z",
                    "Mountpoint": str(self.live),
                    "Labels": {"com.docker.compose.project": "demo"},
                }
            },
        }
        self.state["model"] = {
            "services": {
                "app": {
                    "image": "fixture/app:1",
                    "volumes": [{"type": "volume", "source": "database", "target": "/data"}],
                }
            },
            "volumes": {"database": {"name": "srvr-b2-fixture"}},
        }
        self.state_file = self.tmp / "docker-state.json"
        self.executable = self.tmp / "docker-process.py"
        self.executable.write_text(DOCKER)
        self.write_state()
        self.shell = VolumeShell(self.executable, self.state_file)
        self.svc = self.service(data_volumes=["srvr-b2-fixture"])
        self.env = mock.patch.dict(
            os.environ, {"DOCKER_HOST": "", "DOCKER_CONTEXT": "", "COMPOSE_PROFILES": ""}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_state(self) -> None:
        self.state_file.write_text(json.dumps(self.state))

    def take(self, service=None):
        return backups.create(
            self.shell, self.paths, service or self.svc, reason="manual", version="1"
        )

    def test_volume_only_round_trip_preserves_root_and_replaced_data(self) -> None:
        taken = self.take()
        inode = self.live.stat().st_ino
        (self.live / "database").write_text("after")
        self.live.chmod(0o750)
        kept = backups.restore(self.shell, self.svc, taken)
        self.assertEqual(self.live.stat().st_ino, inode)
        self.assertEqual((self.live / "database").read_text(), "before")
        self.assertEqual((self.live / ".hidden").read_text(), "hidden")
        self.assertEqual((self.live / "link").readlink(), Path("database"))
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.live / "database").stat().st_mode & 0o777, 0o600)
        self.assertEqual((kept[0] / "database").read_text(), "after")
        self.assertEqual(kept[0].stat().st_mode & 0o777, 0o750)
        self.assertFalse((taken.path.parent / ".restore.json").exists())
        self.assertFalse(
            any(call[0] == "mv" and call[-2] == str(self.live) for call in self.shell.calls)
        )

    def test_mixed_directory_and_volume_restore_is_one_transaction(self) -> None:
        directory = self.tmp / "uploads"
        directory.mkdir()
        (directory / "file").write_text("before upload")
        service = self.service(data=[str(directory)], data_volumes=list(self.svc.data_volumes))
        taken = self.take(service)
        (directory / "file").write_text("current upload")
        (self.live / "database").write_text("current database")
        run = self.shell.run
        failed = False

        def fail_install(argv, **kwargs):
            nonlocal failed
            if (
                argv[0] == "mv"
                and ".srvr-restore-" in str(argv[-2])
                and str(argv[-1]).startswith(str(self.live) + "/")
                and not failed
            ):
                failed = True
                raise SrvrError("injected install failure")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_install),
            self.assertRaisesRegex(SrvrError, "install failure"),
        ):
            backups.restore(self.shell, service, taken)
        self.assertEqual((directory / "file").read_text(), "current upload")
        self.assertEqual((self.live / "database").read_text(), "current database")

    def test_snapshot_contains_private_volume_identity_and_preserves_last_copy(self) -> None:
        dest = self.tmp / "snapshots"
        result = snapshots.copy(self.shell, self.svc, dest)
        mirrored = result / self.live.relative_to("/")
        self.assertEqual((mirrored / "database").read_text(), "before")
        metadata = result / ".srvr-data.json"
        self.assertEqual(metadata.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(metadata.read_text())["data_volumes"][0]["name"], self.svc.data_volumes[0]
        )
        (self.live / "database").write_text("after")
        run = self.shell.run

        def fail_copy(argv, **kwargs):
            if argv[0] == "cp":
                raise SrvrError("injected copy failure")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_copy),
            self.assertRaises(SrvrError),
        ):
            snapshots.copy(self.shell, self.svc, dest)
        self.assertEqual((mirrored / "database").read_text(), "before")

    def test_snapshot_restore_uses_staged_switch_and_retains_live_volume_root(self) -> None:
        source = snapshots.copy(self.shell, self.svc, self.tmp / "snapshots")
        saved = snapshots.read(self.shell, self.svc, source)
        inode = self.live.stat().st_ino
        (self.live / "database").write_text("current")
        journal = self.paths.service_backups("demo") / ".restore.json"
        kept = snapshots.restore(self.shell, self.svc, saved, journal)
        self.assertEqual((self.live / "database").read_text(), "before")
        self.assertEqual((kept[0] / "database").read_text(), "current")
        self.assertEqual(self.live.stat().st_ino, inode)
        self.assertEqual((source / self.live.relative_to("/") / "database").read_text(), "before")

    def test_snapshot_restore_rejects_missing_or_changed_manifest(self) -> None:
        source = snapshots.copy(self.shell, self.svc, self.tmp / "snapshots")
        saved = snapshots.read(self.shell, self.svc, source)
        manifest = source / ".srvr-data.json"
        meta = json.loads(manifest.read_text())
        meta["data_volumes"][0]["name"] = "unknown"
        manifest.write_text(json.dumps(meta))
        journal = self.paths.service_backups("demo") / ".restore.json"
        with self.assertRaisesRegex(SrvrError, "changed"):
            snapshots.restore(self.shell, self.svc, saved, journal)
        manifest.unlink()
        with self.assertRaisesRegex(SrvrError, "manifest"):
            snapshots.read(self.shell, self.svc, source)
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_snapshot_restore_rejects_data_changed_after_selection(self) -> None:
        source = snapshots.copy(self.shell, self.svc, self.tmp / "snapshots")
        saved = snapshots.read(self.shell, self.svc, source)
        (source / self.live.relative_to("/") / "database").write_text("changed")
        journal = self.paths.service_backups("demo") / ".restore.json"
        with self.assertRaisesRegex(SrvrError, "snapshot changed"):
            snapshots.restore(self.shell, self.svc, saved, journal)
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_backup_and_snapshot_resume_only_previously_running_containers(self) -> None:
        self.svc.workdir.mkdir()
        self.state["containers"] = {
            "fixture": {"project": "demo", "running": True, "paused": False}
        }
        self.write_state()
        ctx = Context(
            paths=self.paths, shell=self.shell, ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        ctx.services = lambda names: [self.svc]
        self.assertEqual(backup.run(Namespace(services=["demo"], all=False), ctx), 0)
        self.assertEqual(
            snapshot.run(Namespace(services=["demo"], all=False, to=self.tmp / "snapshots"), ctx), 0
        )
        final = json.loads(self.state_file.read_text())
        self.assertTrue(final["containers"]["fixture"]["running"])
        self.assertEqual(final["actions"], ["stop", "start", "stop", "start"])

    def test_remote_daemon_is_refused_before_data_mutation(self) -> None:
        self.state["endpoint"] = "ssh://example.invalid"
        self.write_state()
        with self.assertRaisesRegex(SrvrError, "local"):
            self.take()
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_public_backup_snapshot_and_snapshot_restore_support_volume_only_services(self) -> None:
        self.svc.workdir.mkdir()
        ctx = Context(
            paths=self.paths, shell=self.shell, ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        ctx.service = lambda name: self.svc
        ctx.services = lambda names: [self.svc]
        self.assertEqual(backup.run(Namespace(services=["demo"], all=False), ctx), 0)
        self.assertEqual(len(backups.list_backups(self.paths, "demo")), 1)
        dest = self.tmp / "snapshots"
        self.assertEqual(snapshot.run(Namespace(services=["demo"], all=False, to=dest), ctx), 0)
        self.assertFalse(
            any("start" in call or "up" in call or "stop" in call for call in self.shell.calls)
        )
        (self.live / "database").write_text("current")
        self.assertEqual(
            restore.run(
                Namespace(service="demo", backup="latest", snapshot=dest / "demo", yes=True), ctx
            ),
            0,
        )
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_command_rejects_registry_change_during_confirmation_before_stopping(self) -> None:
        self.svc.workdir.mkdir()
        compose_file = self.svc.compose_files[0]
        compose_file.write_text("original config")
        taken = self.take()
        run = self.shell.run
        renders = 0

        def change_during_confirmation(argv, **kwargs):
            nonlocal renders
            result = run(argv, **kwargs)
            if "config" in argv:
                renders += 1
                if renders == 1:
                    compose_file.write_text("changed config")
            return result

        ctx = Context(
            paths=self.paths, shell=self.shell, ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        ctx.service = lambda name: self.svc
        with (
            mock.patch.object(self.shell, "run", side_effect=change_during_confirmation),
            self.assertRaisesRegex(SrvrError, "configuration changed"),
        ):
            restore.run(Namespace(service="demo", backup=taken.name, yes=True), ctx)
        self.assertFalse(any("stop" in call for call in self.shell.calls))

    def test_snapshot_refuses_overlap_and_symlink_substitution(self) -> None:
        for dest in (self.live / "copy", self.live.parent / "copy"):
            with self.subTest(dest=dest), self.assertRaisesRegex(SrvrError, "overlap"):
                snapshots.copy(self.shell, self.svc, dest)
        link = self.tmp / "link"
        link.symlink_to(self.live, target_is_directory=True)
        with self.assertRaisesRegex(SrvrError, "symlink"):
            snapshots.copy(self.shell, self.svc, link / "copy")

    def test_driver_options_and_mountpoint_are_refused_without_mutation(self) -> None:
        original = dict(self.state["volumes"][self.svc.data_volumes[0]])
        for changes in (
            {"Driver": "nfs"},
            {"Options": {"type": "none", "device": "/etc", "o": "bind"}},
            {"Scope": "global"},
            {"Mountpoint": "/var/lib/docker"},
        ):
            self.state["volumes"][self.svc.data_volumes[0]] = {**original, **changes}
            self.write_state()
            with self.subTest(changes=changes), self.assertRaises(SrvrError):
                self.take()
        self.assertEqual((self.live / "database").read_text(), "before")
        self.assertFalse(any(call[0] in ("mv", "rm", "cp") for call in self.shell.calls))

    def test_symlink_volume_root_is_refused(self) -> None:
        other = self.live.with_name("substituted")
        self.live.rename(other)
        self.live.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(SrvrError, "symlink"):
            self.take()

    def test_external_and_running_containers_are_refused(self) -> None:
        for container in (
            {"project": "outside", "running": False, "paused": False},
            {"project": "demo", "running": True, "paused": False},
            {"project": "demo", "running": False, "paused": True},
        ):
            self.state["containers"] = {"fixture-container": container}
            self.write_state()
            with self.subTest(container=container), self.assertRaises(SrvrError):
                self.take()
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_same_host_recreated_volume_and_unknown_mapping_are_refused(self) -> None:
        taken = self.take()
        self.live.parent.rename(self.live.parent.with_name("retained-volume"))
        self.live.mkdir(parents=True)
        self.state["volumes"][self.svc.data_volumes[0]]["CreatedAt"] = "2026-10-02T00:00:00Z"
        self.write_state()
        with self.assertRaisesRegex(SrvrError, "replaced"):
            backups.restore(self.shell, self.svc, taken)
        taken.meta["data_volumes"][0]["name"] = "unknown"
        with self.assertRaisesRegex(SrvrError, "unknown"):
            backups.restore(self.shell, self.svc, taken)

    def test_changed_engine_creation_timestamp_does_not_reject_unchanged_storage(self) -> None:
        taken = self.take()
        (self.live / "database").write_text("current")
        self.state["volumes"][self.svc.data_volumes[0]]["CreatedAt"] = "2026-10-14T00:00:00Z"
        self.write_state()
        backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_archive_refuses_replaced_storage_with_unchanged_docker_metadata(self) -> None:
        taken = self.take()
        original = self.live.with_name("original-storage")
        self.live.rename(original)
        self.live.mkdir()
        (self.live / "database").write_text("substituted")
        with self.assertRaisesRegex(SrvrError, "storage was replaced"):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.live / "database").read_text(), "substituted")
        self.assertEqual((original / "database").read_text(), "before")

    def test_interrupt_after_installing_content_recovers_original_and_keeps_failed_data(
        self,
    ) -> None:
        taken = self.take()
        (self.live / "database").write_text("current")
        run = self.shell.run
        interrupted = False
        inode = self.live.stat().st_ino

        def interrupt_install(argv, **kwargs):
            nonlocal interrupted
            result = run(argv, **kwargs)
            if (
                argv[0] == "mv"
                and ".srvr-restore-" in str(argv[-2])
                and str(argv[-1]).startswith(str(self.live) + "/")
                and not interrupted
            ):
                interrupted = True
                raise KeyboardInterrupt
            return result

        with (
            mock.patch.object(self.shell, "run", side_effect=interrupt_install),
            self.assertRaises(KeyboardInterrupt),
        ):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.live / "database").read_text(), "current")
        self.assertEqual(self.live.stat().st_ino, inode)
        self.assertFalse((taken.path.parent / ".restore.json").exists())
        kept = list(self.live.parent.glob("_data.failed-restore-*"))
        self.assertEqual(len(kept), 1)
        self.assertTrue(list(kept[0].iterdir()))

    def test_host_rebuild_maps_stable_name_to_a_new_data_root(self) -> None:
        taken = self.take()
        self.root = self.tmp / "rebuilt-docker"
        rebuilt = self.root / "volumes" / self.svc.data_volumes[0] / "_data"
        rebuilt.mkdir(parents=True)
        (rebuilt / "database").write_text("rebuilt data")
        self.state["root"] = str(self.root)
        self.state["daemon"] = "new-daemon"
        self.state["volumes"][self.svc.data_volumes[0]].update(
            {"Mountpoint": str(rebuilt), "CreatedAt": "2026-10-14T00:00:00Z"}
        )
        self.write_state()
        kept = backups.restore(self.shell, self.svc, taken)
        self.assertEqual((rebuilt / "database").read_text(), "before")
        self.assertEqual((kept[0] / "database").read_text(), "rebuilt data")
        self.assertEqual((self.live / "database").read_text(), "before")

    def test_old_archive_cannot_silently_omit_registered_volumes(self) -> None:
        directory = self.tmp / "directory"
        directory.mkdir()
        (directory / "data").write_text("old")
        service = self.service(data=[str(directory)])
        taken = self.take(service)
        taken.meta.pop("data_volumes")
        with self.assertRaisesRegex(SrvrError, "coverage"):
            backups.restore(
                self.shell,
                self.service(data=[str(directory)], data_volumes=list(self.svc.data_volumes)),
                taken,
            )
        backups.restore(self.shell, service, taken)
        self.assertEqual((directory / "data").read_text(), "old")

    def test_shared_registration_is_rejected(self) -> None:
        self.paths.services_dir.mkdir(parents=True)
        common = 'kind = "image"\ndata_volumes = ["srvr-b2-fixture"]\n'
        file = self.paths.services_dir / "demo.toml"
        file.write_text(common + f"workdir = {json.dumps(str(self.svc.workdir))}\n")
        (self.paths.services_dir / "other.toml").write_text(
            common + f"workdir = {json.dumps(str(self.tmp / 'other'))}\n"
        )
        from srvr.core.registry import load_service

        with self.assertRaisesRegex(SrvrError, "ownership"):
            self.take(load_service(file))

    def test_process_death_recovers_partial_evacuation_before_bad_archive_check(self) -> None:
        taken = self.take()
        (self.live / "database").write_text("current")
        code = f"""
import json, os
from pathlib import Path
from srvr.core import backups
from srvr.core.registry import parse_service
from tests.test_named_volumes import VolumeShell
class CrashShell(VolumeShell):
    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if argv[0] == "mv" and str(argv[-2]).startswith({str(self.live)!r} + "/"):
            os._exit(70)
        return result
spec = {{"kind":"image", "workdir":{str(self.svc.workdir)!r},
        "data_volumes":["srvr-b2-fixture"]}}
service = parse_service("demo", spec, Path("unused"))
taken = backups.Backup(Path({str(taken.path)!r}), json.loads({json.dumps(taken.meta)!r}))
shell = CrashShell(Path({str(self.executable)!r}), Path({str(self.state_file)!r}))
backups.restore(shell, service, taken)
"""
        result = subprocess.run(
            [sys.executable, "-c", code], timeout=30, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 70, result.stderr)
        journal = taken.path.parent / ".restore.json"
        self.assertTrue(journal.exists())
        with (
            self.assertRaisesRegex(SrvrError, "unfinished restore"),
            service_lock(self.paths, "demo"),
        ):
            pass
        with taken.path.open("ab") as archive:
            archive.write(b"damaged")
        with self.assertRaisesRegex(SrvrError, "checksum"):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.live / "database").read_text(), "current")
        self.assertFalse(journal.exists())

    def test_recovery_failure_keeps_journal_then_retry_preserves_data(self) -> None:
        taken = self.take()
        (self.live / "database").write_text("current")
        run = self.shell.run

        def fail_moves_to_live(argv, **kwargs):
            if argv[0] == "mv" and str(argv[-1]).startswith(str(self.live) + "/"):
                raise SrvrError("injected destination failure")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_moves_to_live),
            self.assertRaisesRegex(SrvrError, "recovery incomplete"),
        ):
            backups.restore(self.shell, self.svc, taken)
        self.assertTrue((taken.path.parent / ".restore.json").exists())
        with taken.path.open("ab") as archive:
            archive.write(b"damaged")
        with self.assertRaisesRegex(SrvrError, "checksum"):
            backups.restore(self.shell, self.svc, taken)
        self.assertEqual((self.live / "database").read_text(), "current")

    def test_interrupted_journal_refuses_changed_volume_storage(self) -> None:
        taken = self.take()
        run = self.shell.run

        def fail_install_and_recovery(argv, **kwargs):
            if argv[0] == "mv" and str(argv[-1]).startswith(str(self.live) + "/"):
                raise SrvrError("injected failure")
            return run(argv, **kwargs)

        with (
            mock.patch.object(self.shell, "run", side_effect=fail_install_and_recovery),
            self.assertRaises(SrvrError),
        ):
            backups.restore(self.shell, self.svc, taken)
        original = self.live.with_name("original-storage")
        self.live.rename(original)
        self.live.mkdir()
        with self.assertRaisesRegex(SrvrError, "identity changed"):
            backups.restore(self.shell, self.svc, taken)
        self.assertTrue((taken.path.parent / ".restore.json").exists())

    def test_all_startup_commands_respect_named_volume_restore_journal(self) -> None:
        self.svc.workdir.mkdir()
        journal = self.paths.service_backups("demo") / ".restore.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}")
        ctx = Context(
            paths=self.paths, shell=self.shell, ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        ctx.service = lambda name: self.svc
        ctx.services = lambda names: [self.svc]
        for command, args in (
            (start, Namespace(services=["demo"], all=False)),
            (restart, Namespace(services=["demo"], all=False)),
            (compose, Namespace(service="demo", compose_args=["up"])),
            (compose, Namespace(service="demo", compose_args=["run", "app"])),
        ):
            with (
                self.subTest(command=command.__name__),
                self.assertRaisesRegex(SrvrError, "unfinished restore"),
            ):
                command.run(args, ctx)
        self.assertFalse(any("compose" in call for call in self.shell.calls))

    def test_broken_journal_symlink_keeps_startup_and_restore_blocked(self) -> None:
        taken = self.take()
        journal = taken.path.parent / ".restore.json"
        journal.symlink_to(self.tmp / "missing-journal")
        with (
            self.assertRaisesRegex(SrvrError, "unfinished restore"),
            service_lock(self.paths, "demo"),
        ):
            pass
        with self.assertRaisesRegex(SrvrError, "journal is a symlink"):
            backups.restore(self.shell, self.svc, taken)
        self.assertTrue(journal.is_symlink())
        self.assertEqual((self.live / "database").read_text(), "before")
