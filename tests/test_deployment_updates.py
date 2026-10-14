from __future__ import annotations

import hashlib
import io
import json
import os
import re
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path
from unittest import mock

from srvr.commands import rollback, update
from srvr.core import configsnapshots, deploys, envfile
from srvr.core.composefile import find_image
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.shell import Result
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest


def image_id(ref: str) -> str:
    return "sha256:" + hashlib.sha256(ref.encode()).hexdigest()


class DeploymentShell(NoSudoShell):
    """Process-boundary Docker substitute; files and Git remain real."""

    def __init__(self, project: str) -> None:
        super().__init__()
        self.project = project
        self.events: list[tuple[str, list[str]]] = []
        self.models: list[tuple[str, dict]] = []
        self.loaded = {ref: image_id(ref) for ref in ("app:1", "worker:1", "db:1", "migrate:1")}
        self.running = {name: self.loaded[f"{name}:1"] for name in ("app", "worker", "db")}
        self.stopped = False
        self.missing: set[str] = set()
        self.fail_up = False
        self.after_pull = None
        self.before_build = None

    def ensure_sudo(self) -> None:
        pass

    def _model(self, argv: list[str]) -> dict:
        files = [Path(argv[i + 1]) for i, value in enumerate(argv) if value == "--file"]
        work = Path(argv[argv.index("--project-directory") + 1])
        env = Path(argv[argv.index("--env-file") + 1]) if "--env-file" in argv else work / ".env"
        values = dict(envfile.parse(env.read_text())) if env.exists() else {}
        model = {"services": {}}
        for file in files:
            text = file.read_text()
            if text.lstrip().startswith("{"):
                source = json.loads(text)
            else:
                names = re.findall(r"^  ([a-z]+):\s*$", text, re.M)
                source = {
                    "services": {name: {"image": find_image(text, name)[1]} for name in names}
                }
            model.update({key: value for key, value in source.items() if key != "services"})
            for name, spec in source.get("services", {}).items():
                model["services"].setdefault(name, {}).update(deepcopy(spec))
        for spec in model["services"].values():
            if "image" in spec:
                spec["image"] = re.sub(
                    r"\$\{([^}]+)\}", lambda m: values.get(m[1], ""), spec["image"]
                )
            if "build" in spec:
                build = spec["build"]
                build = {"context": build} if isinstance(build, str) else build
                build["context"] = str((files[0].parent / build["context"]).resolve())
                spec["build"] = build
            for file_spec in spec.get("env_file", []):
                path = Path(file_spec if isinstance(file_spec, str) else file_spec["path"])
                path = path if path.is_absolute() else files[0].parent / path
                spec.setdefault("environment", {}).update(dict(envfile.parse(path.read_text())))
            spec.pop("env_file", None)
        return model

    def _compose(self, argv):
        result = Result(0, "", "")
        index = argv.index("compose") + 1
        while argv[index].startswith("--"):
            index += 2
        action = argv[index]
        tail = argv[index + 1 :]
        self.events.append((action, argv))
        model = self._model(argv)
        self.models.append((action, deepcopy(model)))
        if action == "config":
            result = Result(0, json.dumps(model), "")
        elif action in ("pull", "build"):
            components = [name for name in tail if name in model["services"]]
            if action == "build" and self.before_build:
                self.before_build(argv)
            for name in components:
                ref = model["services"][name].get("image", f"{self.project}-{name}")
                if ref in self.missing:
                    result = Result(1, "", "secret preparation diagnostic")
                    break
                self.loaded[ref] = image_id(ref + ("-built" if action == "build" else ""))
            if action == "pull" and self.after_pull:
                self.after_pull()
        elif action == "stop":
            self.stopped = True
        elif action == "up":
            if self.fail_up:
                self.fail_up = False
                result = Result(1, "", "secret switch diagnostic")
            else:
                self.running = {
                    name: spec.get("image", f"{self.project}-{name}")
                    for name, spec in model["services"].items()
                    if name != "migrate" and not spec.get("profiles")
                }
                self.running = {
                    name: self.loaded.get(ref, ref) for name, ref in self.running.items()
                }
                self.stopped = False
        elif action == "run" and (
            "--pull" not in tail or tail[tail.index("--pull") + 1] != "never"
        ):
            raise AssertionError("migration allowed an implicit pull")
        return result

    def run(self, argv, **kwargs):
        argv = [str(arg) for arg in argv]
        check = kwargs.get("check", True)
        result = Result(0, "", "")
        if "compose" in argv and "docker" in argv:
            result = self._compose(argv)
        elif argv[:3] == ["docker", "ps", "--quiet"]:
            result = Result(0, "", "")
        elif argv[:3] == ["docker", "ps", "--all"]:
            state = "exited" if self.stopped else "running"
            result = Result(
                0,
                "\n".join(
                    f"{self.project}\t{name}\t{name}\t{state}\tUp (healthy)\tcid-{name}"
                    for name in self.running
                ),
                "",
            )
        elif argv[:2] == ["docker", "inspect"]:
            result = Result(
                0,
                "\n".join(f"{cid} {self.running[cid.removeprefix('cid-')]}" for cid in argv[4:]),
                "",
            )
        elif argv[:3] == ["docker", "image", "tag"]:
            self.loaded[argv[4]] = argv[3]
            self.events.append(("tag", argv))
        elif argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1]
            result = (
                Result(0, self.loaded[ref], "") if ref in self.loaded else Result(1, "", "missing")
            )
        else:
            return super().run(argv, **kwargs)
        if check and not result.ok:
            raise SrvrError(result.stderr)
        return result


class TransactionalDeploymentTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.file = self.work / "compose.yaml"
        self.model = {
            "services": {name: {"image": f"{name}:1"} for name in ("app", "worker", "db")}
        }
        self.file.write_text(json.dumps(self.model))
        self.service_ = self.service()
        self.shell = DeploymentShell(self.service_.project)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.ctx = Context(paths=self.paths, shell=self.shell, ui=UI(out=self.out, err=self.err))
        self.ctx.service = lambda _name: self.service_
        self.ctx.services = lambda _names: [self.service_]

    def args(self, *options):
        parser = ArgumentParser()
        update.configure(parser)
        return parser.parse_args(["demo", "--yes", "--no-backup", *options])

    def apply(self, *options):
        return update.run(self.args(*options), self.ctx)

    def actions(self):
        return [event for event, _argv in self.shell.events]

    def capture(self):
        self.assertEqual(self.apply("--capture-config"), 0)

    def test_batch_prepares_only_selected_images_and_keeps_unrelated_running_id(self):
        db_before = self.shell.running["db"]
        original = self.file.read_bytes()
        self.assertEqual(
            self.apply("--set-image", "app=app:2", "--set-image", "worker=worker:2"), 0
        )
        self.assertEqual(
            self.shell.running,
            {"app": image_id("app:2"), "worker": image_id("worker:2"), "db": db_before},
        )
        pulls = [argv for action, argv in self.shell.events if action == "pull"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(set(pulls[0][-2:]), {"app", "worker"})
        self.assertEqual(self.file.read_bytes(), original)
        self.assertLess(self.actions().index("pull"), self.actions().index("stop"))
        self.assertEqual(self.actions().count("up"), 1)
        record = deploys.load_record(self.paths, "demo")
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.after.images, self.shell.running)
        up = next(argv for action, argv in self.shell.events if action == "up")
        self.assertIn("--no-build", up)
        self.assertEqual(up[-2:], ["--pull", "never"])

    def test_missing_batch_image_leaves_config_containers_and_prior_record_untouched(self):
        self.assertEqual(self.apply("--component", "app", "--image", "app:2"), 0)
        previous = deploys.load_record(self.paths, "demo")
        original = self.file.read_bytes()
        running = dict(self.shell.running)
        rollback_tags = {
            key: value for key, value in self.shell.loaded.items() if key.startswith("srvr-prev/")
        }
        self.shell.events.clear()
        self.shell.missing.add("worker:missing")
        with self.assertRaisesRegex(SrvrError, "preparation failed"):
            self.apply("--set-image", "app=app:3", "--set-image", "worker=worker:missing")
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(self.shell.running, running)
        self.assertNotIn("stop", self.actions())
        self.assertEqual(deploys.load_record(self.paths, "demo"), previous)
        self.assertEqual(
            {
                key: value
                for key, value in self.shell.loaded.items()
                if key.startswith("srvr-prev/")
            },
            rollback_tags,
        )
        self.assertNotIn("secret preparation diagnostic", self.out.getvalue() + self.err.getvalue())

    def test_env_shared_expression_uses_private_owned_override_without_changing_other_images(self):
        env = self.work / ".env"
        env.write_text("SHARED=app:1\nSECRET_TOKEN=private-value\n")
        self.model["services"]["app"]["image"] = "${SHARED}"
        self.model["services"]["worker"]["image"] = "${SHARED}"
        self.file.write_text(json.dumps(self.model))
        self.shell.running["worker"] = image_id("app:1")
        before = self.file.read_bytes(), env.read_bytes()
        self.assertEqual(self.apply("--component", "app", "--image", "app:2"), 0)
        self.assertEqual((self.file.read_bytes(), env.read_bytes()), before)
        self.assertEqual(self.shell.running["worker"], image_id("app:1"))
        owned = configsnapshots.version_file(self.paths, self.service_)
        self.assertEqual(owned.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(owned.read_text()), {"services": {"app": {"image": "app:2"}}})
        self.assertNotIn("private-value", self.out.getvalue() + self.err.getvalue())

    def test_config_candidate_is_staged_and_failed_switch_restores_every_registered_file(self):
        env = self.work / ".env"
        config = self.work / "app.env"
        env.write_text("APP_VERSION=app:1\n")
        config.write_text("SECRET_TOKEN=old-private-value\n")
        config.chmod(0o640)
        self.service_ = self.service(config_files=[str(config)])
        self.model["services"]["app"] = {"image": "${APP_VERSION}", "env_file": [str(config)]}
        self.file.write_text(json.dumps(self.model))
        self.capture()
        originals = configsnapshots.capture(self.service_)
        compose_candidate, env_candidate, config_candidate = (
            self.tmp / name for name in ("compose-next", "env-next", "config-next")
        )
        next_model = deepcopy(self.model)
        next_model["services"]["worker"]["image"] = "worker:2"
        compose_candidate.write_text(json.dumps(next_model))
        env_candidate.write_text("APP_VERSION=app:2\n")
        config_candidate.write_text("SECRET_TOKEN=new-private-value\n")
        self.shell.fail_up = True
        self.assertEqual(
            self.apply(
                "--config-file",
                f"{self.file}={compose_candidate}",
                "--config-file",
                f"{env}={env_candidate}",
                "--config-file",
                f"{config}={config_candidate}",
            ),
            1,
        )
        restored = configsnapshots.capture(self.service_)
        for path, state in originals.items():
            self.assertEqual(
                (restored[path].content, restored[path].mode, restored[path].mtime_ns),
                (state.content, state.mode, state.mtime_ns),
            )
        self.assertEqual(self.shell.running["app"], image_id("app:1"))
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "rolled-back")
        self.assertNotIn("private-value", self.out.getvalue() + self.err.getvalue())
        self.assertFalse(
            (self.paths.service_state("demo") / "configuration-recovery.json").exists()
        )

    def test_config_candidate_preparation_failure_keeps_original_disk_files(self):
        self.capture()
        candidate = self.tmp / "candidate.json"
        model = deepcopy(self.model)
        model["services"]["app"]["image"] = "app:missing"
        candidate.write_text(json.dumps(model))
        original = self.file.read_bytes()
        baseline = configsnapshots.baseline_file(self.paths, self.service_).read_bytes()
        self.shell.missing.add("app:missing")
        self.shell.events.clear()
        with self.assertRaisesRegex(SrvrError, "preparation failed"):
            self.apply("--config-file", f"{self.file}={candidate}")
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(
            configsnapshots.baseline_file(self.paths, self.service_).read_bytes(), baseline
        )
        self.assertNotIn("stop", self.actions())

    def test_preedited_configuration_rolls_back_to_protected_baseline(self):
        self.capture()
        original = self.file.read_bytes()
        self.model["services"]["app"]["image"] = "app:2"
        self.file.write_text(json.dumps(self.model))
        self.shell.fail_up = True
        self.assertEqual(self.apply("--config"), 1)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(self.shell.running["app"], image_id("app:1"))

    def test_manual_config_rollback_restores_all_files_and_captured_images(self):
        env = self.work / ".env"
        env.write_text("APP_VERSION=app:1\nSECRET_TOKEN=old-private\n")
        self.model["services"]["app"]["image"] = "${APP_VERSION}"
        self.file.write_text(json.dumps(self.model))
        self.capture()
        original = env.read_bytes()
        env.write_text("APP_VERSION=app:2\nSECRET_TOKEN=new-private\n")
        self.assertEqual(self.apply("--config"), 0)
        parser = ArgumentParser()
        rollback.configure(parser)
        args = parser.parse_args(["demo", "--yes"])
        self.assertEqual(rollback.run(args, self.ctx), 0)
        self.assertEqual(env.read_bytes(), original)
        self.assertEqual(self.shell.running["app"], image_id("app:1"))
        self.assertNotIn("private", self.out.getvalue().replace("privately", ""))

    def test_changed_config_baseline_candidate_registry_and_symlink_plans_are_refused(self):
        self.capture()
        candidate = self.tmp / "next.json"
        model = deepcopy(self.model)
        model["services"]["app"]["image"] = "app:2"
        candidate.write_text(json.dumps(model))
        args = self.args("--config-file", f"{self.file}={candidate}")
        for target in (
            candidate,
            configsnapshots.baseline_file(self.paths, self.service_),
            self.service_.source,
        ):
            if target == self.service_.source:
                target.write_text("initial registry\n")
                configsnapshots.save_baseline(
                    self.paths,
                    self.service_,
                    configsnapshots.capture(
                        self.service_,
                        extra=(configsnapshots.version_file(self.paths, self.service_),),
                    ),
                )
            plan = update.plan_configuration(self.ctx, self.service_, args)
            original = target.read_bytes()
            target.write_bytes(original + b"\n")
            with self.assertRaisesRegex(SrvrError, "changed"):
                plan.validate()
            target.write_bytes(original)
        link = self.work / "linked.env"
        link.symlink_to(candidate)
        service = self.service(config_files=[str(link)])
        with self.assertRaisesRegex(SrvrError, "symlinks"):
            configsnapshots.capture(service)

    def test_changes_during_prepare_prevent_stop_and_preserve_concurrent_edit(self):
        concurrent = self.file.read_text() + "\n"
        self.shell.after_pull = lambda: self.file.write_text(concurrent)
        with self.assertRaisesRegex(SrvrError, "preparation failed"):
            self.apply("--component", "app", "--image", "app:2")
        self.assertEqual(self.file.read_text(), concurrent)
        self.assertNotIn("stop", self.actions())

    def test_snapshot_restore_failure_is_journaled_and_retry_preserves_concurrent_changes(self):
        original = configsnapshots.capture(self.service_)
        target = dict(original)
        target[str(self.file)] = configsnapshots.content(
            original[str(self.file)], b'{"services": {"app": {"image": "app:2"}}}'
        )
        from srvr.core import atomic

        journal = self.paths.service_state("demo") / "configuration-recovery.json"
        atomic.write_json(
            journal,
            {
                "target": configsnapshots.encode(target),
                "expected": configsnapshots.encode(original),
            },
        )
        with (
            mock.patch.object(Path, "replace", side_effect=OSError("disk failure")),
            self.assertRaises(OSError),
        ):
            configsnapshots.recover(self.paths, self.service_)
        self.assertTrue(journal.exists())

        self.file.write_text("concurrent configuration\n")
        with self.assertRaisesRegex(SrvrError, "concurrent"):
            configsnapshots.recover(self.paths, self.service_)
        self.assertEqual(self.file.read_text(), "concurrent configuration\n")
        self.assertTrue(journal.exists())

    def test_configuration_read_ignores_access_time_updates(self):
        os.utime(self.file, ns=(0, 0))
        first = configsnapshots.read(self.file)
        self.assertEqual(first.mtime_ns, 0)
        self.assertEqual(configsnapshots.read(self.file), first)

    def test_old_snapshot_records_still_restore_the_primary_compose_text(self):
        snapshot = deploys.Snapshot(
            images=dict(self.shell.running), compose_text=self.file.read_text()
        )
        self.file.write_text('{"services": {"app": {"image": "app:2"}}}')
        deploys.apply(self.ctx, self.service_, snapshot)
        self.assertEqual(self.file.read_text(), snapshot.compose_text)
        self.assertEqual(self.shell.running, snapshot.images)

    def test_volume_only_service_takes_one_backup_with_unchanged_calling_convention(self):
        self.service_ = self.service(data_volumes=["srvr-test-volume"])
        self.model["services"]["db"]["volumes"] = [{"type": "volume", "source": "db"}]
        self.model["volumes"] = {"db": {"name": "srvr-test-volume"}}
        self.file.write_text(json.dumps(self.model))
        args = self.args("--set-image", "app=app:2", "--set-image", "worker=worker:2")
        args.no_backup = False
        taken = mock.Mock(name="backup")
        taken.name, taken.size = "backup-1", 10
        with (
            mock.patch.object(update.backups, "create", return_value=taken) as create,
            mock.patch.object(update.backups, "prune"),
        ):
            self.assertEqual(update.run(args, self.ctx), 0)
        create.assert_called_once_with(
            self.shell, self.paths, self.service_, reason="update", version="app:1, db:1, worker:1"
        )
        self.assertEqual(self.actions().count("stop"), 1)


class GitPreparationTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.file = self.work / "compose.yaml"
        self.service_ = self.service()
        self.shell = DeploymentShell(self.service_.project)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.ctx = Context(paths=self.paths, shell=self.shell, ui=UI(out=self.out, err=self.err))
        self.ctx.service = lambda _name: self.service_
        self.ctx.services = lambda _names: [self.service_]

    args = TransactionalDeploymentTest.args
    apply = TransactionalDeploymentTest.apply
    actions = TransactionalDeploymentTest.actions
    capture = TransactionalDeploymentTest.capture

    def git(self, *args):
        return self.shell.run(["git", "-C", str(self.work), *args]).stdout.strip()

    def source_stack(self, *, hybrid=False):
        self.service_ = self.service(kind="git", branch="main", migrate="migrate")
        self.model = {
            "services": {
                "app": {"build": {"context": "."}},
                "worker": {"image": "worker:1"},
                "db": {"image": "db:1"},
                "migrate": {"image": "migrate:1", "profiles": ["tools"]},
            }
        }
        if hybrid:
            self.model["services"]["app"].update(image="app:1", pull_policy="always")
        self.file.write_text(json.dumps(self.model))
        (self.work / "Dockerfile").write_text("FROM scratch\n")
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "old")
        before = self.git("rev-parse", "HEAD")
        (self.work / "Dockerfile").write_text("FROM scratch\nLABEL version=2\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "new")
        target = self.git("rev-parse", "HEAD")
        remote = self.tmp / "remote.git"
        self.shell.run(["git", "clone", "--quiet", "--bare", str(self.work), str(remote)])
        self.git("remote", "add", "origin", str(remote))
        self.git("reset", "--hard", "--quiet", before)
        return before, target

    def test_git_builds_and_pulls_before_moving_checkout_and_pins_migration(self):
        before, target = self.source_stack()
        original = self.file.read_bytes()
        self.shell.before_build = lambda _argv: self.assertEqual(
            self.git("rev-parse", "HEAD"), before
        )
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), target)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.file.read_bytes(), original)
        self.assertLess(self.actions().index("build"), self.actions().index("stop"))
        self.assertLess(self.actions().index("pull"), self.actions().index("stop"))
        runtime = json.loads((self.paths.service_state("demo") / "images.compose.json").read_text())
        self.assertEqual(runtime["services"]["migrate"]["image"], image_id("migrate:1"))
        run = next(argv for action, argv in self.shell.events if action == "run")
        self.assertEqual(run[-3:], ["--pull", "never", "migrate"])

    def test_missing_git_prebuilt_image_keeps_original_checkout_and_old_stack(self):
        before, _target = self.source_stack()
        self.shell.missing.add("db:1")
        running = dict(self.shell.running)
        with self.assertRaisesRegex(SrvrError, "preparation failed"):
            self.apply()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.shell.running, running)
        self.assertNotIn("stop", self.actions())

    def test_git_configuration_route_refuses_changes_to_repository_owned_compose(self):
        self.source_stack()
        self.capture()
        candidate = self.tmp / "compose-next.json"
        changed = deepcopy(self.model)
        changed["services"]["db"]["image"] = "db:2"
        candidate.write_text(json.dumps(changed))
        with self.assertRaisesRegex(SrvrError, "repository-owned"):
            self.apply("--config-file", f"{self.file}={candidate}")

    def test_hybrid_source_builds_local_image_and_never_pulls_published_application(self):
        self.source_stack(hybrid=True)
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.shell.running["app"], image_id("app:1-built"))
        build = next(model for action, model in self.shell.models if action == "build")
        self.assertEqual(build["services"]["app"]["pull_policy"], "build")
        pulls = [argv for action, argv in self.shell.events if action == "pull"]
        self.assertTrue(pulls)
        self.assertTrue(all("app" not in argv for argv in pulls))

    def test_failed_hybrid_source_build_never_falls_back_to_cached_published_image(self):
        before, _ = self.source_stack(hybrid=True)
        running = dict(self.shell.running)
        self.shell.missing.add("app:1")
        with self.assertRaisesRegex(SrvrError, "preparation failed"):
            self.apply()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.shell.running, running)
        self.assertNotIn("pull", self.actions())
        self.assertNotIn("stop", self.actions())


class HybridImageDeploymentTest(TempDirTest):
    args = TransactionalDeploymentTest.args
    apply = TransactionalDeploymentTest.apply
    actions = TransactionalDeploymentTest.actions

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.file = self.work / "compose.yaml"
        self.model = {
            "services": {name: {"image": f"{name}:1"} for name in ("app", "worker", "db")}
        }
        self.service_ = self.service()
        self.shell = DeploymentShell(self.service_.project)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.ctx = Context(paths=self.paths, shell=self.shell, ui=UI(out=self.out, err=self.err))
        self.ctx.service = lambda _name: self.service_
        self.ctx.services = lambda _names: [self.service_]
        for name in ("app", "worker"):
            self.model["services"][name].update(
                build={"context": "missing-source", "dockerfile": "absent"}, pull_policy="build"
            )
        self.model["services"]["disabled"] = {
            "build": {"context": "unavailable"},
            "profiles": ["debug"],
        }
        self.file.write_text(json.dumps(self.model))

    def test_image_batch_pulls_hybrids_and_runtime_has_no_build_or_pull_policy_escape(self):
        original = self.file.read_bytes()
        self.assertEqual(
            self.apply("--set-image", "app=app:2", "--set-image", "worker=worker:2"), 0
        )
        self.assertNotIn("build", self.actions())
        self.assertEqual(self.shell.running["app"], image_id("app:2"))
        self.assertEqual(self.shell.running["worker"], image_id("worker:2"))
        self.assertEqual(self.file.read_bytes(), original)
        for action, model in self.shell.models:
            if action in ("pull", "up", "run"):
                self.assertNotIn("disabled", model["services"])
                for spec in model["services"].values():
                    self.assertNotIn("build", spec)
                    self.assertEqual(spec["pull_policy"], "always" if action == "pull" else "never")

    def test_failed_hybrid_pull_preserves_prior_record_config_containers_and_rollback_images(self):
        self.assertEqual(self.apply("--component", "app", "--image", "app:2"), 0)
        record = deploys.load_record(self.paths, "demo")
        original = self.file.read_bytes()
        running = dict(self.shell.running)
        protected = {k: v for k, v in self.shell.loaded.items() if k.startswith("srvr-prev/")}
        self.shell.events.clear()
        self.shell.missing.add("worker:missing")
        with self.assertRaisesRegex(SrvrError, "preparation failed"):
            self.apply("--set-image", "app=app:3", "--set-image", "worker=worker:missing")
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(self.shell.running, running)
        self.assertEqual(deploys.load_record(self.paths, "demo"), record)
        self.assertEqual(
            {k: v for k, v in self.shell.loaded.items() if k.startswith("srvr-prev/")}, protected
        )
        self.assertNotIn("build", self.actions())
        self.assertNotIn("stop", self.actions())

    def test_required_build_only_or_unpinned_hybrid_ref_is_refused_before_mutation(self):
        for spec in (
            {"build": {"context": "."}},
            {"build": {"context": "."}, "image": "app:latest"},
        ):
            self.model["services"]["app"] = spec
            self.file.write_text(json.dumps(self.model))
            self.shell.events.clear()
            with self.subTest(spec=spec), self.assertRaises(SrvrError):
                self.apply("--component", "worker", "--image", "worker:2")
            self.assertTrue(set(self.actions()) <= {"config"})

    def test_hybrid_switch_failure_restores_captured_ids_and_configuration(self):
        original = self.file.read_bytes()
        running = dict(self.shell.running)
        self.shell.fail_up = True
        self.assertEqual(self.apply("--component", "app", "--image", "app:2"), 1)
        self.assertEqual(self.file.read_bytes(), original)
        self.assertEqual(self.shell.running, running)
        self.assertEqual(deploys.load_record(self.paths, "demo").status, "rolled-back")
        self.assertNotIn("build", self.actions())
