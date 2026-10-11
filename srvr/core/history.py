"""Append-only per-service event log (`~/.local/state/srvr/<service>/history.jsonl`)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .paths import Paths, ensure_private_dir, keep_home_owner
from .ui import iso, utc_now


@dataclass(frozen=True)
class Event:
    time: str
    service: str
    action: str  # update, rollback, restore, backup, snapshot
    result: str  # ok, failed, rolled-back
    before: str = ""  # version before (commit or image)
    after: str = ""  # version after
    backup: str = ""  # backup name involved, if any
    note: str = ""


def record(paths: Paths, service: str, action: str, result: str, **details: str) -> Event:
    event = Event(time=iso(utc_now()), service=service, action=action, result=result, **details)
    directory = ensure_private_dir(paths.service_state(service))
    file = directory / "history.jsonl"
    with file.open("a") as log:
        log.write(json.dumps(asdict(event)) + "\n")
    keep_home_owner(file)
    return event


def read(paths: Paths, service: str, *, limit: int | None = None) -> list[Event]:
    """Newest first."""
    file = paths.service_state(service) / "history.jsonl"
    if not file.exists():
        return []
    events = []
    for line in file.read_text().splitlines():
        try:
            events.append(Event(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue  # skip a torn or foreign line rather than fail the whole log
    events.reverse()
    return events[:limit] if limit else events
