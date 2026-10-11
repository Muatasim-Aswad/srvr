from pathlib import Path

from srvr.core.errors import SrvrError
from srvr.core.registry import Registry, load_registry, select
from tests.helpers import TempDirTest

VOCAB = """
kind = "git"
workdir = "/srv/vocab"
branch = "main"
env_file = "/etc/vocab.env"
compose_files = ["compose.yaml", "/etc/override.yaml"]
migrate = "migrate"
data = ["/var/lib/vocab"]
health = "http://127.0.0.1:3000/health"
"""


class RegistryTest(TempDirTest):
    def write(self, name: str, text: str) -> None:
        self.paths.services_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.services_dir / f"{name}.toml").write_text(text)

    def test_parses_and_resolves_paths(self) -> None:
        self.write("vocab", VOCAB)
        service = load_registry(self.paths.services_dir).services["vocab"]
        self.assertEqual(service.project, "vocab")
        self.assertEqual(
            service.compose_files, (Path("/srv/vocab/compose.yaml"), Path("/etc/override.yaml"))
        )
        self.assertEqual(service.config_files, (Path("/etc/vocab.env"),))
        self.assertTrue(service.is_git)

    def test_defaults(self) -> None:
        service = self.service()
        self.assertEqual(service.compose_files, (self.tmp / "work" / "compose.yaml",))
        self.assertEqual((service.env_file, service.config_files, service.data), (None, (), ()))

    def test_rejects_invalid_entries(self) -> None:
        cases = {
            "unknown key": {"colour": "red"},
            "git without branch": {"kind": "git"},
            "image with branch": {"branch": "main"},
            "bad kind": {"kind": "helm"},
            "relative data": {"data": ["data"]},
            "too broad data": {"data": ["/home"]},
            "non-url health": {"health": "localhost:80"},
            "image with version_url": {"version_url": "http://127.0.0.1/version.json"},
            "non-url version_url": {
                "kind": "git",
                "branch": "main",
                "version_url": "127.0.0.1/version.json",
            },
            "wrong type": {"data": "/var/lib/x"},
            "empty compose files": {"compose_files": []},
            "overlapping data": {"data": ["/var/lib/app", "/var/lib/app/uploads"]},
            "duplicate data": {"data": ["/var/lib/app", "/var/lib/app"]},
            "git update component": {"kind": "git", "branch": "main", "update_component": "app"},
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(SrvrError):
                self.service(**raw)

    def test_rejects_bad_names(self) -> None:
        with self.assertRaises(SrvrError):
            self.service("Bad_Name")

    def test_select(self) -> None:
        registry = Registry({"a": self.service("a"), "b": self.service("b")}, {})
        self.assertEqual([s.name for s in select(registry, [])], ["a", "b"])
        self.assertEqual([s.name for s in select(registry, ["b", "b"])], ["b"])
        with self.assertRaisesRegex(SrvrError, "unknown service"):
            select(registry, ["c"])

    def test_broken_file_affects_only_its_service(self) -> None:
        self.write("vocab", VOCAB)
        self.write("memos", 'kind = "helm"\nworkdir = "/srv/memos"\n')
        self.write("tududi", "not = [valid toml")
        registry = load_registry(self.paths.services_dir)
        self.assertEqual(list(registry.services), ["vocab"])
        self.assertEqual(sorted(registry.broken), ["memos", "tududi"])
        self.assertEqual([s.name for s in select(registry, [])], ["vocab"])
        with self.assertRaisesRegex(SrvrError, "memos: broken service file"):
            select(registry, ["memos"])

    def test_missing_registry(self) -> None:
        with self.assertRaisesRegex(SrvrError, "no service registry"):
            load_registry(self.paths.services_dir)

    def test_conflicting_registrations_cannot_use_separate_service_locks(self) -> None:
        for conflict in (
            'project = "vocab"',
            'workdir = "/srv/vocab"',
            'data = ["/var/lib/vocab/uploads"]',
        ):
            with self.subTest(conflict=conflict):
                self.write("vocab", VOCAB)
                raw = 'kind = "image"\n'
                if not conflict.startswith("workdir"):
                    raw += 'workdir = "/srv/other"\n'
                self.write("other", raw + conflict + "\n")
                registry = load_registry(self.paths.services_dir)
                self.assertEqual(set(registry.broken), {"vocab", "other"})
                self.assertFalse(registry.services)
