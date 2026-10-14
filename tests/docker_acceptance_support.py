"""Run public srvr commands against isolated projects on the local Docker daemon."""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "deployment"
DOCKERFILE = """\
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG APP_REVISION
ARG APP_COMMIT_TIME
ARG APP_BUILD_TIME
ENV APP_REVISION=${APP_REVISION} APP_COMMIT_TIME=${APP_COMMIT_TIME} APP_BUILD_TIME=${APP_BUILD_TIME}
COPY app.py database.py /fixture/
CMD ["python", "/fixture/app.py"]
"""


class DockerAcceptanceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="srvr-acceptance-")
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        temporary = self.tmp / "temporary"
        temporary.mkdir()
        self.project = "srvr-acceptance-" + uuid.uuid4().hex[:12]
        self.package_root = Path(
            os.environ.get("SRVR_TEST_PACKAGE_ROOT", FIXTURES.parents[2])
        ).resolve()
        self.docker_binary = shutil.which("docker")
        self.python_binary = os.environ.get("SRVR_TEST_PYTHON", sys.executable)
        self.env = {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_DATA_HOME": str(self.tmp / "share"),
            "TMPDIR": str(temporary),
            "NO_COLOR": "1",
            "COMPOSE_ANSI": "never",
            "COMPOSE_MENU": "false",
        }
        self.install_adapters()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.work = self.tmp / "checkout"
        self.upstream = self.tmp / "upstream"
        self.images = set()
        self.registry = None
        self.base = self.project + "/base:1"
        self.app_image = self.project + "/app:deploy"
        self.addCleanup(self.remove_resources)
        self.base_id = self.docker(
            "image",
            "inspect",
            os.environ.get("SRVR_TEST_BASE_IMAGE", "python:3.14.6-slim-trixie"),
            "--format",
            "{{.Id}}",
        ).stdout.strip()
        self.start_registry()
        self.base = f"{self.registry_host}/{self.project}/base:1"
        self.docker("image", "tag", self.base_id, self.base)
        self.images.update((self.base, self.app_image))
        self.docker("push", self.base)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.env_file = self.tmp / "config.env"
        self.env_file.write_text(
            f"TEST_DATA_DIR={self.data}\nTEST_BASE_IMAGE={self.base}\n"
            f"TEST_APP_IMAGE={self.app_image}\nTEST_PORT={self.port}\n"
            f"TEST_USER={os.getuid()}:{os.getgid()}\n"
            f"TEST_DATABASE_SCRIPT={FIXTURES / 'database.py'}\n"
        )
        settings = self.tmp / "config" / "srvr" / "srvr.toml"
        settings.parent.mkdir(parents=True)
        settings.write_text("health_timeout = 12\nmigration_timeout = 60\n")

    def install_adapters(self):
        binaries = self.tmp / "bin"
        binaries.mkdir()
        sudo = binaries / "sudo"
        sudo.write_text('#!/bin/sh\ncase "$1" in\n-n|--) shift;;\n-v) exit 0;;\nesac\nexec "$@"\n')
        sudo.chmod(0o700)
        compose = os.environ.get("SRVR_TEST_COMPOSE")
        docker = binaries / "docker"
        selection = (
            'if [ "$1" = compose ]; then shift; exec ' + shlex.quote(compose) + ' "$@"; fi\n'
            if compose
            else ""
        )
        docker.write_text(
            "#!/bin/sh\n" + selection + "exec " + shlex.quote(self.docker_binary) + ' "$@"\n'
        )
        docker.chmod(0o700)
        self.env["PATH"] = str(binaries) + os.pathsep + os.environ["PATH"]

    def command(self, argv, *, cwd=None, check=True, timeout=120):
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"{shlex.join(map(str, argv))}\n{result.stdout}\n{result.stderr}")
        return result

    def docker(self, *args, check=True):
        return self.command(["docker", *args], check=check)

    def srvr(self, *args, check=True):
        return self.command(
            [self.python_binary, "-m", "srvr", *args], cwd=self.package_root, check=check
        )

    def git(self, work, *args):
        return self.command(
            [
                "git",
                "-C",
                str(work),
                "-c",
                "user.name=srvr test",
                "-c",
                "user.email=srvr-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ]
        ).stdout.strip()

    def commit(self, *, version="one", schema=1, fail_migration=False, fail_health=False):
        code = (FIXTURES / "app.py").read_text()
        code = code.replace('VERSION = "one"', f'VERSION = "{version}"')
        code = code.replace("SCHEMA_VERSION = 1", f"SCHEMA_VERSION = {schema}")
        code = code.replace("FAIL_MIGRATION = False", f"FAIL_MIGRATION = {fail_migration}")
        code = code.replace("FAIL_HEALTH = False", f"FAIL_HEALTH = {fail_health}")
        (self.upstream / "app.py").write_text(code)
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "--quiet", "-m", f"App {version}")
        return self.git(self.upstream, "rev-parse", "HEAD")

    def compose_text(self):
        return """\
services:
  db:
    image: ${TEST_BASE_IMAGE:?}
    pull_policy: never
    user: ${TEST_USER:?}
    command: [python, /fixture/database.py]
    volumes:
      - ${TEST_DATA_DIR:?}:/data
      - ${TEST_DATABASE_SCRIPT:?}:/fixture/database.py:ro
    stop_grace_period: 1s
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/state')"]
      interval: 1s
      timeout: 2s
      retries: 10
  app: &app
    image: ${TEST_APP_IMAGE:?}
    build:
      context: .
      args:
        BASE_IMAGE: ${TEST_BASE_IMAGE:?}
        APP_REVISION: ${APP_REVISION:-}
        APP_COMMIT_TIME: ${APP_COMMIT_TIME:-}
        APP_BUILD_TIME: ${APP_BUILD_TIME:-}
    user: ${TEST_USER:?}
    volumes: ["${TEST_DATA_DIR:?}:/data"]
    ports: ["127.0.0.1:${TEST_PORT:?}:8000"]
    depends_on:
      db:
        condition: service_healthy
    stop_grace_period: 1s
  migrate:
    image: ${TEST_APP_IMAGE:?}
    build:
      context: .
      args:
        BASE_IMAGE: ${TEST_BASE_IMAGE:?}
        APP_REVISION: ${APP_REVISION:-}
        APP_COMMIT_TIME: ${APP_COMMIT_TIME:-}
        APP_BUILD_TIME: ${APP_BUILD_TIME:-}
    user: ${TEST_USER:?}
    volumes: ["${TEST_DATA_DIR:?}:/data"]
    command: [python, /fixture/app.py, migrate]
    profiles: [tools]
"""

    def prepare_source(self, *options):
        self.upstream.mkdir()
        self.git(self.upstream, "init", "--quiet", "--initial-branch=main")
        (self.upstream / "database.py").write_text((FIXTURES / "database.py").read_text())
        (self.upstream / "Dockerfile").write_text(DOCKERFILE)
        (self.upstream / "compose.yaml").write_text(self.compose_text())
        (self.upstream / ".dockerignore").write_text(".git\n")
        first = self.commit()
        self.git(self.tmp, "clone", "--quiet", str(self.upstream), str(self.work))
        self.srvr(
            "add",
            "example",
            "--workdir",
            str(self.work),
            "--kind",
            "git",
            "--branch",
            "main",
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--data",
            str(self.data),
            "--migrate",
            "migrate",
            "--health",
            f"http://127.0.0.1:{self.port}/health",
            "--version-url",
            f"http://127.0.0.1:{self.port}/version.json",
            *options,
            "-y",
        )
        self.srvr("update", "example", "--force", "-y")
        return first

    def response(self, path="/state"):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            return json.load(response)

    def start_registry(self):
        if self.registry:
            return
        self.registry = self.project + "-registry"
        self.docker(
            "run",
            "--detach",
            "--name",
            self.registry,
            "--publish",
            "127.0.0.1::5000",
            "--env",
            "OTEL_TRACES_EXPORTER=none",
            "--env",
            "REGISTRY_LOG_FORMATTER=json",
            os.environ.get("SRVR_TEST_REGISTRY_IMAGE", "registry:3"),
        )
        port = self.docker("port", self.registry, "5000/tcp").stdout.strip().rsplit(":", 1)[1]
        self.registry_host = f"localhost:{port}"
        self.wait_http(f"http://127.0.0.1:{port}/v2/")

    def prepare_registry(self, *, register=True):
        self.start_registry()
        self.work.mkdir()
        (self.work / "Dockerfile").write_text(DOCKERFILE)
        (self.work / "database.py").write_text((FIXTURES / "database.py").read_text())
        refs = [f"{self.registry_host}/{self.project}/app:{version}" for version in (1, 2)]
        database = f"{self.registry_host}/{self.project}/db:1"
        for version, ref in zip(("one", "two"), refs, strict=True):
            code = (
                (FIXTURES / "app.py")
                .read_text()
                .replace('VERSION = "one"', f'VERSION = "{version}"')
            )
            (self.work / "app.py").write_text(code)
            self.docker(
                "build",
                "--quiet",
                "--build-arg",
                f"BASE_IMAGE={self.base}",
                "--tag",
                ref,
                str(self.work),
            )
            self.images.add(ref)
            self.docker("push", ref)
        self.docker("image", "tag", self.base_id, database)
        self.images.add(database)
        self.docker("push", database)
        source = self.compose_text()
        database_section = source[source.index("  db:") : source.index("  app:")]
        database_section = database_section.replace(
            "image: ${TEST_BASE_IMAGE:?}", f"image: {database}"
        )
        database_section = database_section.replace("    pull_policy: never\n", "")
        app = f"""\
  app:
    image: {refs[0]}
    user: ${{TEST_USER:?}}
    volumes: ["${{TEST_DATA_DIR:?}}:/data"]
    ports: ["127.0.0.1:${{TEST_PORT:?}}:8000"]
    depends_on:
      db:
        condition: service_healthy
    stop_grace_period: 1s
"""
        (self.work / "compose.yaml").write_text("services:\n" + database_section + app)
        if not register:
            return refs, database
        self.srvr(
            "add",
            "example",
            "--workdir",
            str(self.work),
            "--kind",
            "image",
            "--project",
            self.project,
            "--env-file",
            str(self.env_file),
            "--data",
            str(self.data),
            "--update-component",
            "app",
            "--health",
            f"http://127.0.0.1:{self.port}/health",
            "--start",
            "--backup",
            "-y",
        )
        return refs, database

    def registry_manifest_reads(self, component, tag):
        uri = f"/v2/{self.project}/{component}/manifests/{tag}"
        logs = self.docker("logs", self.registry)
        count = 0
        for line in (logs.stdout + logs.stderr).splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("http.request.uri") == uri and entry.get("http.request.method") in (
                "GET",
                "HEAD",
            ):
                count += 1
        return count

    def running_images(self):
        ids = self.docker(
            "ps", "--quiet", "--filter", f"label=com.docker.compose.project={self.project}"
        ).stdout.split()
        records = json.loads(self.docker("inspect", *ids).stdout)
        return {
            item["Config"]["Labels"]["com.docker.compose.service"]: item["Image"]
            for item in records
        }

    def running_containers(self):
        return sorted(
            self.docker(
                "ps", "--quiet", "--filter", f"label=com.docker.compose.project={self.project}"
            ).stdout.split()
        )

    def remove_resources(self):
        ids = self.docker(
            "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}", check=False
        ).stdout.split()
        if self.registry:
            ids.append(self.registry)
        if ids:
            self.docker("rm", "--force", "--volumes", *ids, check=False)
        self.docker("network", "rm", self.project + "_default", check=False)
        images = [
            *self.images,
            *(
                f"{namespace}/{self.project}-{name}:latest"
                for namespace in ("srvr-prev", "srvr-preparing", "srvr-ready")
                for name in ("app", "db", "migrate", "worker", "disabled")
            ),
        ]
        self.docker("image", "rm", *images, check=False)

    def wait_http(self, url):
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    return response.read()
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
