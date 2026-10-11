"""Minimal, format-preserving edits to a Compose file's `image:` lines.

Only the one value changes, so comments and layout in hand-written files survive."""

from __future__ import annotations

import re

from .errors import SrvrError

_KEY = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+):(?P<rest>.*)$")
_IMAGE_VALUE = re.compile(r"^(?P<pre>\s*image:\s*)(?P<value>.*?)(?P<post>\s*(#.*)?)$")


def validate_image_ref(ref: str) -> None:
    """Accept only explicitly pinned references: a non-latest tag and/or a digest."""
    if any(char.isspace() for char in ref) or not ref:
        raise SrvrError(f"invalid image reference: {ref!r}")
    name, _, digest = ref.partition("@")
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SrvrError(f"invalid digest in {ref!r}")
    last = name.rsplit("/", 1)[-1]
    tag = last.partition(":")[2]
    if not digest and not tag:
        raise SrvrError(f"{ref!r} is not pinned; give an exact tag and/or @sha256 digest")
    if tag == "latest":
        raise SrvrError(f"{ref!r} uses 'latest'; pin an exact version")


def _significant(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def find_image(text: str, component: str) -> tuple[int, str]:
    """Line index and current value of `services.<component>.image`."""
    lines = text.splitlines()
    in_services = False
    component_indent: int | None = None
    child_indent: int | None = None
    for index, line in enumerate(lines):
        if not _significant(line):
            continue
        indent = _indent(line)
        match = _KEY.match(line)
        if indent == 0:
            in_services = bool(match and match["key"] == "services")
            component_indent = None
            continue
        if not in_services:
            continue
        if component_indent is not None and indent <= component_indent:
            component_indent = None  # left the component block
        if component_indent is None:
            if match and match["key"] == component and not match["rest"].strip():
                component_indent, child_indent = indent, None
            continue
        if child_indent is None:
            child_indent = indent
        if indent == child_indent and match and match["key"] == "image":
            value = _IMAGE_VALUE.match(line)
            assert value is not None
            return index, value["value"].strip("\"'")
    raise SrvrError(f"no image: line for service {component!r}")


def set_image(text: str, component: str, ref: str) -> str:
    """Return `text` with `services.<component>.image` set to `ref`, keeping quoting."""
    index, _ = find_image(text, component)
    lines = text.splitlines(keepends=True)
    line = lines[index]
    newline = line[len(line.rstrip("\r\n")) :]
    match = _IMAGE_VALUE.match(line.rstrip("\r\n"))
    assert match is not None
    quote = match["value"][0] if match["value"][:1] in ("'", '"') else ""
    lines[index] = f"{match['pre']}{quote}{ref}{quote}{match['post']}{newline}"
    return "".join(lines)
