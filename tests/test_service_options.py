"""Registered Compose selection, networking and persistent volume coverage."""

import json
import os
from unittest import mock

from srvr.core.compose import Compose
from srvr.core.errors import SrvrError
from srvr.core.onboarding import named_volumes, validate_model, validate_unique
from srvr.core.registry import conflict, load_registry
from srvr.core.shell import Result
from tests.helpers import TempDirTest


class ServiceOptionsTest(TempDirTest):
    def test_old_registry_defaults_and_has_data(self) -> None:
        service = self.service()
        self.assertEqual((service.profiles, service.data_volumes), ((), ()))
        self.assertFalse(service.allow_public_ports)
        self.assertFalse(service.allow_host_network)
        self.assertFalse(service.has_data)
        self.assertTrue(self.service(data=["/srv/app/data"]).has_data)
        self.assertTrue(self.service(data_volumes=["demo_db"]).has_data)

    def test_registry_validates_names_lists_and_boolean_opt_ins(self) -> None:
        for raw in (
            {"profiles": ["*"]},
            {"profiles": ["-tools"]},
            {"profiles": ["tools", "tools"]},
            {"profiles": ["tools,worker"]},
            {"profiles": [1]},
            {"data_volumes": ["/var/lib/docker/volumes/db/_data"]},
            {"data_volumes": ["db", "db"]},
            {"data_volumes": ["a"]},
            {"data_volumes": ["../db"]},
            {"allow_public_ports": "true"},
            {"allow_host_network": 1},
        ):
            with self.subTest(raw=raw), self.assertRaises(SrvrError):
                self.service(**raw)
        service = self.service(profiles=["worker", "Tools_2.0"], data_volumes=["demo.db-2"])
        self.assertEqual(service.profiles, ("worker", "Tools_2.0"))

    def test_volume_ownership_conflicts_even_with_distinct_projects(self) -> None:
        first = self.service("one", workdir=str(self.tmp / "one"), data_volumes=["shared_db"])
        second = self.service("two", workdir=str(self.tmp / "two"), data_volumes=["shared_db"])
        self.assertEqual(conflict(first, second), "named volumes")
        self.paths.services_dir.mkdir(parents=True)
        for service in (first, second):
            (self.paths.services_dir / f"{service.name}.toml").write_text(
                f'kind = "image"\nworkdir = "{service.workdir}"\ndata_volumes = ["shared_db"]\n'
            )
        registry = load_registry(self.paths.services_dir)
        self.assertFalse(registry.services)
        self.assertEqual(set(registry.broken), {"one", "two"})
        with self.assertRaisesRegex(SrvrError, "repair broken"):
            validate_unique(self.paths, self.service("three"))

    def test_network_opt_ins_are_independent_and_warn_without_values(self) -> None:
        for ip in (None, "0.0.0.0", "192.0.2.4", "::"):
            model = {"services": {"app": {"image": "app:1", "ports": [{"host_ip": ip}]}}}
            with self.subTest(ip=ip):
                with self.assertRaisesRegex(SrvrError, "127.0.0.1"):
                    validate_model(self.service(), model)
                with self.assertRaises(SrvrError):
                    validate_model(self.service(allow_host_network=True), model)
                warnings = validate_model(self.service(allow_public_ports=True), model)
                self.assertEqual(len(warnings), 1)
                self.assertNotIn(str(ip), warnings[0])
        host = {"services": {"app": {"image": "app:1", "network_mode": "host"}}}
        with self.assertRaisesRegex(SrvrError, "allow_host_network"):
            validate_model(self.service(allow_public_ports=True), host)
        self.assertIn("bypasses", validate_model(self.service(allow_host_network=True), host)[0])
        for ip in ("127.0.0.1", "::1"):
            self.assertFalse(
                validate_model(
                    self.service(),
                    {"services": {"app": {"image": "app:1", "ports": [{"host_ip": ip}]}}},
                )
            )

    def test_all_profile_validation_does_not_ignore_unsafe_inactive_components(self) -> None:
        model = {
            "services": {
                "app": {"image": "app:1"},
                "debug": {"image": "debug:1", "profiles": ["debug"], "network_mode": "host"},
            }
        }
        with self.assertRaises(SrvrError):
            validate_model(self.service(), model)

    def test_resolved_selected_volumes_and_coverage(self) -> None:
        model = {
            "services": {
                "app": {
                    "image": "app:1",
                    "volumes": [
                        {"type": "volume", "source": "db"},
                        {"type": "volume", "source": "external"},
                        {"type": "volume"},
                    ],
                },
                "worker": {
                    "image": "worker:1",
                    "profiles": ["worker"],
                    "volumes": [
                        {"type": "volume", "source": "jobs"},
                        {"type": "bind", "source": "/srv/worker/data"},
                    ],
                },
            },
            "volumes": {
                "db": {"name": "demo_db"},
                "external": {"name": "existing_db", "external": True},
                "jobs": {"name": "demo_jobs"},
            },
        }
        self.assertEqual(
            named_volumes(model), {"db": "demo_db", "external": "existing_db", "jobs": "demo_jobs"}
        )
        warnings = validate_model(self.service(data_volumes=["demo_db", "existing_db"]), model)
        self.assertEqual(warnings, ["app: anonymous volume is unmanaged and not backed up"])
        for names in (["db"], ["demo_jobs"], ["unmounted_db"]):
            with (
                self.subTest(names=names),
                self.assertRaisesRegex(SrvrError, "resolved named volumes"),
            ):
                validate_model(self.service(data_volumes=names), model)
        warnings = validate_model(
            self.service(profiles=["worker"], data_volumes=["demo_jobs"]), model
        )
        self.assertTrue(any("writable bind" in warning for warning in warnings))
        self.assertTrue(any("named volume" in warning for warning in warnings))
        self.assertFalse(any("jobs" in warning for warning in warnings))

    def test_unknown_profiles_and_unselected_registered_components_are_refused(self) -> None:
        model = {
            "services": {
                "app": {"image": "app:1"},
                "worker": {"image": "worker:1", "profiles": ["worker"]},
            }
        }
        for raw in (
            {"profiles": ["missing"]},
            {"migrate": "missing"},
            {"update_component": "worker"},
        ):
            with self.subTest(raw=raw), self.assertRaises(SrvrError):
                validate_model(self.service(**raw), model)


class ComposeSelectionTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.model = {
            "services": {
                "app": {"image": "app:1"},
                "worker": {"image": "worker:1", "profiles": ["worker"]},
                "debug": {"image": "debug:1", "profiles": ["debug"]},
            }
        }
        self.shell = mock.Mock()
        self.shell.run.return_value = Result(0, json.dumps(self.model), "")
        self.compose = Compose(self.service(profiles=["worker"]), self.shell)

    def test_operational_model_and_image_refs_use_only_registered_profiles(self) -> None:
        self.assertEqual(set(self.compose.config()["services"]), {"app", "worker"})
        self.assertEqual(self.compose.image_refs(), {"app": "app:1", "worker": "worker:1"})
        self.assertEqual(
            set(self.compose.config(all_profiles=True)["services"]), {"app", "worker", "debug"}
        )
        self.assertEqual(set(Compose(self.service(), self.shell).config()["services"]), {"app"})

    def test_registered_one_off_migration_keeps_legacy_profile_behavior(self) -> None:
        compose = Compose(self.service(migrate="debug"), self.shell)
        compose.run_once("debug", pull=False)
        self.assertEqual(self.shell.run.call_args.args[0][-2:], ["never", "debug"])
        self.assertNotIn("--profile", self.shell.run.call_args.args[0])
        self.assertEqual(set(compose.config()["services"]), {"app"})

    def test_all_operational_commands_use_fixed_profiles_and_clear_dotenv_selection(self) -> None:
        for action in (
            self.compose.build,
            self.compose.pull,
            self.compose.up,
            self.compose.start,
            self.compose.restart,
            self.compose.stop,
            lambda: self.compose.run_once("worker"),
            lambda: self.compose.passthrough(["up", "-d"]),
            lambda: self.compose.passthrough(["config"]),
        ):
            with self.subTest(action=action):
                action()
                argv = self.shell.run.call_args.args[0]
                self.assertEqual(argv[:4], ["env", "COMPOSE_PROFILES=", "docker", "compose"])
                index = argv.index("--profile")
                self.assertEqual(argv[index + 1], "worker")
                self.assertNotIn("*", argv)

    def test_ambient_profiles_are_refused_without_echoing_values(self) -> None:
        with (
            mock.patch.dict(os.environ, {"COMPOSE_PROFILES": "secret-profile"}),
            self.assertRaisesRegex(SrvrError, "unset COMPOSE_PROFILES") as caught,
        ):
            self.compose.build()
        self.assertNotIn("secret-profile", str(caught.exception))
        self.shell.run.assert_not_called()
        self.compose.service.workdir.mkdir()
        (self.compose.service.workdir / ".env").write_text("COMPOSE_PROFILES=debug\nTOKEN=secret\n")
        with self.assertRaisesRegex(SrvrError, "remove COMPOSE_PROFILES"):
            self.compose.argv("up")
        self.shell.run.assert_not_called()

    def test_explicit_env_file_profiles_are_also_refused(self) -> None:
        env = self.tmp / "config.env"
        env.write_text("export COMPOSE_PROFILES='debug'\n")
        compose = Compose(self.service(env_file=str(env)), self.shell)
        with self.assertRaisesRegex(SrvrError, "remove COMPOSE_PROFILES"):
            compose.pull()
        self.shell.run.assert_not_called()

    def test_unselected_targets_and_passthrough_profile_overrides_are_refused(self) -> None:
        for action in (
            lambda: self.compose.pull("debug"),
            lambda: self.compose.run_once("debug"),
            lambda: self.compose.passthrough(["run", "debug"]),
            lambda: self.compose.passthrough(["up", "debug"]),
            lambda: self.compose.passthrough(["--profile", "debug", "up"]),
            lambda: self.compose.passthrough(["up", "--profile=debug"]),
            lambda: self.compose.passthrough(["up", "--file=other.yaml"]),
            lambda: self.compose.passthrough(["up", "-fother.yaml"]),
            lambda: self.compose.passthrough(["up", "--env-file", "other.env"]),
            lambda: self.compose.passthrough(["up", "-pother"]),
            lambda: self.compose.passthrough(["run", "-p", "8080:80", "app"]),
        ):
            with self.subTest(action=action), self.assertRaises(SrvrError):
                action()
        self.assertTrue(all("config" in call.args[0] for call in self.shell.run.call_args_list))
        self.compose.passthrough(["exec", "worker", "echo", "debug", "--profile=debug"])
        self.assertIn("exec", self.shell.run.call_args.args[0])

    def test_config_failures_hide_resolved_secret_diagnostics(self) -> None:
        self.shell.run.return_value = Result(1, "", "resolved PASSWORD=secret-value")
        with self.assertRaisesRegex(SrvrError, "details hidden") as caught:
            self.compose.config()
        self.assertNotIn("secret-value", str(caught.exception))

    def test_mutation_revalidates_network_opt_ins(self) -> None:
        self.model["services"]["debug"]["ports"] = [{"host_ip": "0.0.0.0"}]
        self.shell.run.return_value = Result(0, json.dumps(self.model), "")
        with self.assertRaisesRegex(SrvrError, "127.0.0.1"):
            self.compose.up()
        self.assertTrue(all("config" in call.args[0] for call in self.shell.run.call_args_list))
