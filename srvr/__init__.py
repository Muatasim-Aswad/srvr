"""srvr: manage the Docker Compose services on this server."""

import tomllib
from pathlib import Path

__version__: str = tomllib.loads(
    (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
)["project"]["version"]
