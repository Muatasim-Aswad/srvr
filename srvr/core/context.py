"""Per-invocation context handed to every command."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cached_property

from .compose import Compose
from .docker import Docker
from .errors import SrvrError
from .git import Checkout
from .paths import Paths
from .registry import Registry, Service, load_registry, select
from .settings import Settings
from .shell import Shell
from .ui import UI


class Context:
    def __init__(self, *, paths: Paths, shell: Shell, ui: UI) -> None:
        self.paths = paths
        self.shell = shell
        self.ui = ui
        self._warned_broken = False

    @cached_property
    def settings(self) -> Settings:
        return Settings.load(self.paths.settings_file)

    @cached_property
    def registry(self) -> Registry:
        return load_registry(self.paths.services_dir)

    @cached_property
    def docker(self) -> Docker:
        return Docker(self.shell)

    def services(self, names: Iterable[str]) -> list[Service]:
        """Named services, or every valid service when `names` is empty."""
        names = list(names)
        if not names and self.registry.broken and not self._warned_broken:
            self._warned_broken = True
            for name, error in self.registry.broken.items():
                self.ui.warn(f"{name}: skipped, broken service file: {error}")
        return select(self.registry, names)

    def service(self, name: str) -> Service:
        return select(self.registry, [name])[0]

    def compose(self, service: Service) -> Compose:
        return Compose(
            service,
            self.shell,
            runtime_file=self.paths.service_state(service.name) / "images.compose.json",
        )

    def checkout(self, service: Service) -> Checkout:
        if not service.is_git:
            raise SrvrError(f"{service.name} is not built from Git")
        return Checkout(service.workdir, self.shell)
