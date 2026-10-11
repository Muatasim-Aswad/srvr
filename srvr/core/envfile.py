"""Read dotenv-style files and mask secret values for display."""

from __future__ import annotations

import re

SECRET_KEY = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|PASS\b|KEY|CREDENTIAL|AUTH|PRIVATE", re.I)
URL_WITH_CREDENTIALS = re.compile(r"://[^/\s]*@")


def parse(text: str) -> list[tuple[str, str]]:
    """KEY=VALUE pairs in file order; comments, blanks and malformed lines are skipped."""
    pairs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        pairs.append((key, value))
    return pairs


def display_value(key: str, value: str) -> str:
    """Secret-looking keys never show their value, only whether one is set."""
    if not value:
        return "(empty)"
    if SECRET_KEY.search(key) or URL_WITH_CREDENTIALS.search(value):
        return "(set, hidden)"
    return value
