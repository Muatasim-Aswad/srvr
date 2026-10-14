"""HTTP health probes."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Probe:
    url: str
    ok: bool
    detail: str  # "200 in 12 ms", "HTTP 503", "connection refused", ...


def probe(url: str, *, timeout: float = 5) -> Probe:
    request = urllib.request.Request(url, headers={"User-Agent": "srvr"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return Probe(url, False, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Probe(url, False, str(reason))
    elapsed = round((time.monotonic() - started) * 1000)
    return Probe(url, 200 <= status < 400, f"{status} in {elapsed} ms")


def build_probe(url: str, expected: str, *, timeout: float = 5) -> Probe:
    """Whether the build record at `url` names commit `expected`."""
    request = urllib.request.Request(url, headers={"User-Agent": "srvr"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            revision = json.load(response).get("revision")
    except urllib.error.HTTPError as exc:
        exc.close()
        return Probe(url, False, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return Probe(url, False, str(getattr(exc, "reason", exc)))
    except (ValueError, AttributeError):
        return Probe(url, False, "not a build record")
    if revision == expected:
        return Probe(url, True, f"serves {expected[:7]}")
    return Probe(url, False, f"serves {str(revision)[:7]}, checkout is {expected[:7]}")


def wait_until(check: Callable[[], Probe], *, timeout: float, interval: float = 2) -> Probe:
    """Repeat `check` until it passes or `timeout` seconds elapse; return the last result."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result.ok or time.monotonic() >= deadline:
            return result
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
