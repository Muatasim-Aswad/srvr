"""Readiness and migration ordering at the Compose/Engine process boundary."""

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from srvr.core import deploys
from srvr.core.context import Context
from srvr.core.errors import SrvrError
from srvr.core.shell import Result
from srvr.core.startup import start_prepared
from srvr.core.ui import UI
from tests.helpers import NoSudoShell, TempDirTest


class ReadinessShell(NoSudoShell):
    def __init__(self, file):
        super().__init__()
        self.file = file
        self.states = {}
        self.events = []
        self.sequence = []

    def run(self, argv, **kwargs):
        argv = list(map(str, argv))
        if "compose" in argv:
            if "config" in argv:
                return Result(0, self.file.read_text(), "")
            component = argv[-1]
            self.events.append(("run" if "run" in argv else "up", component, argv))
            if "up" in argv:
                self.states[component] = ("running", "Up (healthy)", "False")
            return Result(0, "", "")
        if argv[:2] == ["docker", "ps"]:
            if self.sequence:
                self.states = self.sequence.pop(0)
            return Result(
                0,
                "\n".join(
                    f"demo\t{name}\t{name}\t{state}\t{status}\tid-{name}\t{oneoff}"
                    for name, (state, status, oneoff) in self.states.items()
                ),
                "",
            )
        return super().run(argv, **kwargs)


class ReadinessSafetyTest(TempDirTest):
    def setUp(self):
        super().setUp()
        work = self.tmp / "work"
        work.mkdir()
        self.file = work / "compose.yaml"
        self.model = {"services": {"app": {"image": "app:1"}, "worker": {"image": "worker:1"}}}
        self.write_model()
        self.shell = ReadinessShell(self.file)
        self.ctx = Context(
            paths=self.paths, shell=self.shell, ui=UI(out=io.StringIO(), err=io.StringIO())
        )
        self.service_ = self.service()

    def write_model(self):
        self.file.write_text(json.dumps(self.model))

    def endpoint(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_http_success_does_not_hide_missing_exited_or_unhealthy_worker(self):
        self.service_ = self.service(health=self.endpoint())
        app = ("running", "Up (healthy)", "False")
        for worker in (
            None,
            ("exited", "Exited (0)", "False"),
            ("running", "Up (unhealthy)", "False"),
        ):
            with self.subTest(worker=worker):
                self.shell.states = {"app": app}
                if worker:
                    self.shell.states["worker"] = worker
                self.assertFalse(deploys.wait_ready(self.ctx, self.service_, timeout=0).ok)

    def test_starting_health_waits_for_engine_health_and_timeout_is_bounded(self):
        app = ("running", "Up (healthy)", "False")
        self.shell.sequence = [
            {"app": app, "worker": ("running", "Up (health: starting)", "False")},
            {"app": app, "worker": app},
        ]
        self.assertTrue(deploys.wait_ready(self.ctx, self.service_, timeout=4).ok)
        self.shell.states["worker"] = ("running", "Up (health: starting)", "False")
        started = time.monotonic()
        self.assertFalse(deploys.wait_ready(self.ctx, self.service_, timeout=0.05).ok)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_transient_running_worker_does_not_complete_readiness(self):
        app = ("running", "Up", "False")
        self.shell.sequence = [
            {"app": app, "worker": app},
            {"app": app, "worker": ("exited", "Exited (1)", "False")},
        ]
        self.assertFalse(deploys.wait_ready(self.ctx, self.service_, timeout=1.2).ok)

    def test_oneoff_container_cannot_supply_a_missing_replica_and_inactive_profile_is_excluded(
        self,
    ):
        self.model["services"]["worker"]["profiles"] = ["workers"]
        self.write_model()
        self.shell.states = {
            "app": ("running", "Up", "False"),
            "worker": ("exited", "Exited", "False"),
        }
        self.assertTrue(deploys.container_probe(self.ctx, self.service_).ok)
        self.shell.states["worker"] = ("running", "Up", "True")
        self.assertFalse(deploys.container_probe(self.ctx, self.service(profiles=["workers"])).ok)

    def test_migration_prerequisite_is_ready_before_job_and_app_and_rollback_skips_job(self):
        self.model = {
            "services": {
                "app": {
                    "image": "app:1",
                    "depends_on": {"migrate": {"condition": "service_completed_successfully"}},
                },
                "db": {"image": "db:1"},
                "migrate": {
                    "image": "migrate:1",
                    "depends_on": {"db": {"condition": "service_healthy"}},
                },
            }
        }
        self.write_model()
        start_prepared(self.ctx, self.service_)
        self.assertEqual(
            [(action, name) for action, name, _ in self.shell.events],
            [("up", "db"), ("run", "migrate"), ("up", "app")],
        )
        for _, _, argv in self.shell.events:
            self.assertIn("--no-deps", argv)
            self.assertEqual(argv[argv.index("--pull") + 1], "never")
        self.shell.events.clear()
        start_prepared(self.ctx, self.service_, migrate=False)
        self.assertEqual(
            [(action, name) for action, name, _ in self.shell.events], [("up", "db"), ("up", "app")]
        )

    def test_cyclic_or_unselected_prerequisites_fail_before_starting(self):
        self.model["services"]["app"]["depends_on"] = {"worker": {"condition": "service_started"}}
        self.model["services"]["worker"]["profiles"] = ["workers"]
        self.write_model()
        with self.assertRaisesRegex(SrvrError, "outside registered profiles"):
            start_prepared(self.ctx, self.service_)
        self.assertEqual(self.shell.events, [])

    def test_optional_unselected_prerequisite_does_not_change_registered_selection(self):
        self.model["services"]["app"]["depends_on"] = {
            "worker": {"condition": "service_started", "required": False}
        }
        self.model["services"]["worker"]["profiles"] = ["workers"]
        self.write_model()
        self.assertEqual(set(self.ctx.compose(self.service_).config()["services"]), {"app"})

    def test_healthy_prerequisite_requires_engine_health_even_when_model_has_no_healthcheck(self):
        self.shell.states = {
            "app": ("running", "Up", "False"),
            "worker": ("running", "Up", "False"),
        }
        self.assertFalse(deploys.container_probe(self.ctx, self.service_, require_healthy=True).ok)
        self.shell.states = {
            name: ("running", "Up (healthy)", "False") for name in ("app", "worker")
        }
        self.assertTrue(deploys.container_probe(self.ctx, self.service_, require_healthy=True).ok)
