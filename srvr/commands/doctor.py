"""`srvr doctor`: whole-server health report.

Each check is a small function returning `Finding`s, so adding a check is one function plus
one entry in CHECKS."""

from __future__ import annotations

import json
import shutil
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from srvr.commands._shared import docker_access
from srvr.core import backups, history
from srvr.core.context import Context
from srvr.core.docker import summarize
from srvr.core.health import probe
from srvr.core.settings import Settings
from srvr.core.ui import parse_iso, utc_now

HELP = "Check the whole server: units, disk, SSD, memory, containers, health, backups, updates"

OK, WARN, FAIL = "ok", "warn", "fail"

REBOOT_FLAG = Path("/run/reboot-required")
# Sectors the drive could not read; nonzero means data was already at risk.
UNREADABLE_SECTOR_ATTRIBUTES = {197: "pending", 198: "offline uncorrectable"}
REALLOCATED_SECTORS = 5


@dataclass(frozen=True)
class Finding:
    check: str
    level: str
    detail: str


def configure(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--no-sudo", action="store_true", help="skip checks that need sudo rather than prompt"
    )


def check_units(ctx: Context) -> list[Finding]:
    out = ctx.shell.run(["systemctl", "--failed", "--no-legend", "--plain"], check=False).stdout
    units = [line.split()[0] for line in out.splitlines() if line.strip()]
    if units:
        return [Finding("systemd", FAIL, "failed: " + ", ".join(units))]
    return [Finding("systemd", OK, "no failed units")]


def check_disk(ctx: Context) -> list[Finding]:
    usage = shutil.disk_usage("/")
    percent = round(usage.used * 100 / usage.total)
    level = WARN if percent >= ctx.settings.disk_warn_percent else OK
    return [Finding("disk /", level, f"{percent}% used, {usage.free // 2**30} GiB free")]


def check_memory(ctx: Context) -> list[Finding]:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0])  # kB
    available = info["MemAvailable"] * 100 // info["MemTotal"]
    level = WARN if available < 15 else OK
    return [
        Finding("memory", level, f"{available}% available ({info['MemAvailable'] // 1024} MiB)")
    ]


def check_ssd(ctx: Context) -> list[Finding]:
    scan = json.loads(ctx.shell.run(["smartctl", "--scan", "--json"], sudo=True).stdout)
    devices = [device["name"] for device in scan.get("devices", [])]
    if not devices:
        return [Finding("ssd", FAIL, "smartctl found no drive")]
    findings = []
    for device in devices:
        result = ctx.shell.run(
            ["smartctl", "--json", "-H", "-A", "-l", "error", "-l", "selftest", device],
            sudo=True,
            check=False,
        )
        findings += assess_smart(device, json.loads(result.stdout), ctx.settings)
    return findings


def assess_smart(device: str, report: dict[str, Any], settings: Settings) -> list[Finding]:
    """Findings for one drive from `smartctl --json -H -A -l error -l selftest`."""
    if "smart_status" not in report:
        messages = report.get("smartctl", {}).get("messages", [])
        reason = "; ".join(m.get("string", "") for m in messages) or "no SMART data"
        return [Finding("ssd", FAIL, f"{device}: {reason}")]
    attributes = report.get("ata_smart_attributes", {}).get("table", [])
    raw = {a["id"]: a["raw"]["value"] for a in attributes}
    failing = [a["name"] for a in attributes if a.get("when_failed") == "now"]
    findings = []
    if not report["smart_status"].get("passed", False):
        findings.append(Finding("ssd", FAIL, f"{device}: SMART health check failed"))
    if failing:
        findings.append(Finding("ssd", FAIL, f"{device}: below threshold: {', '.join(failing)}"))
    for attribute, label in UNREADABLE_SECTOR_ATTRIBUTES.items():
        if raw.get(attribute, 0) > 0:
            findings.append(Finding("ssd", FAIL, f"{device}: {raw[attribute]} {label} sector(s)"))
    if raw.get(REALLOCATED_SECTORS, 0) > 0:
        findings.append(
            Finding("ssd", WARN, f"{device}: {raw[REALLOCATED_SECTORS]} reallocated sector(s)")
        )
    errors = report.get("ata_smart_error_log", {}).get("summary", {}).get("count", 0)
    if errors:
        findings.append(Finding("ssd", WARN, f"{device}: {errors} error(s) in the drive log"))
    findings += _selftest_findings(device, report, settings)
    hours = report.get("power_on_time", {}).get("hours", "?")
    return findings or [Finding("ssd", OK, f"{device}: healthy, {hours} power-on hours")]


def _selftest_findings(device: str, report: dict[str, Any], settings: Settings) -> list[Finding]:
    tests = report.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
    if not tests:
        return [Finding("ssd", WARN, f"{device}: no self-test has run")]
    newest = tests[0]
    if newest["status"].get("passed") is False:
        return [Finding("ssd", FAIL, f"{device}: last self-test: {newest['status']['string']}")]
    hours_since = report.get("power_on_time", {}).get("hours", 0) - newest["lifetime_hours"]
    if hours_since > settings.selftest_max_age_days * 24:
        return [Finding("ssd", WARN, f"{device}: last self-test {hours_since // 24} days ago")]
    return []


def check_reboot(ctx: Context) -> list[Finding]:
    if not REBOOT_FLAG.exists():
        return [Finding("reboot", OK, "not required")]
    waited = (utc_now() - datetime.fromtimestamp(REBOOT_FLAG.stat().st_mtime, UTC)).days
    level = FAIL if waited >= ctx.settings.reboot_fail_days else WARN
    return [
        Finding("reboot", level, f"required for {waited} day(s) by updates (needs someone on site)")
    ]


def check_apt(ctx: Context) -> list[Finding]:
    out = ctx.shell.run(["apt", "list", "--upgradable"], check=False).stdout
    count = sum(1 for line in out.splitlines() if "/" in line)
    level = WARN if count else OK
    return [Finding("apt", level, f"{count} package(s) upgradable" if count else "up to date")]


def check_tailscale(ctx: Context) -> list[Finding]:
    result = ctx.shell.run(["tailscale", "status", "--json"], check=False)
    state = json.loads(result.stdout).get("BackendState") if result.ok else None
    findings = [Finding("tailscale", OK if state == "Running" else FAIL, f"backend {state}")]
    served = ctx.shell.run(["tailscale", "serve", "status"], check=False).stdout
    for service in ctx.services([]):
        if service.url:
            host = urlsplit(service.url).hostname or ""
            if host not in served:
                findings.append(Finding("serve", FAIL, f"{service.name}: {host} not served"))
    if len(findings) == 1:
        findings.append(Finding("serve", OK, "every registered URL is served"))
    return findings


def check_containers(ctx: Context) -> list[Finding]:
    findings = []
    for service in ctx.services([]):
        exclude = [service.migrate] if service.migrate else []
        state = summarize(ctx.docker.project_containers(service.project, exclude=exclude))
        if state not in ("healthy", "running"):
            findings.append(Finding("containers", FAIL, f"{service.name}: {state}"))
    return findings or [Finding("containers", OK, "all running")]


def check_health(ctx: Context) -> list[Finding]:
    urls = [(s.name, u) for s in ctx.services([]) for u in (s.health, s.url) if u]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda item: probe(item[1]), urls))
    findings = [
        Finding("health", FAIL, f"{name}: {url} {r.detail}")
        for (name, url), r in zip(urls, results, strict=True)
        if not r.ok
    ]
    return findings or [Finding("health", OK, f"{len(urls)} endpoints respond")]


def check_backups(ctx: Context) -> list[Finding]:
    """Newest srvr backup or snapshot per service; srvr cannot see the off-server upload."""
    findings = []
    limit = ctx.settings.backup_max_age_days
    services = [service for service in ctx.services([]) if service.data]
    for service in services:
        if (ctx.paths.service_backups(service.name) / ".restore.json").exists():
            findings.append(Finding("backups", FAIL, f"{service.name}: unfinished restore"))
            continue
        newest = _newest_copy(ctx, service.name)
        if newest is None:
            findings.append(Finding("backups", WARN, f"{service.name}: no backup or snapshot"))
            continue
        age = (utc_now() - newest).days
        if age > limit:
            findings.append(Finding("backups", WARN, f"{service.name}: newest is {age} days old"))
    return findings or [
        Finding("backups", OK, f"{len(services)} services copied within {limit} days")
    ]


def _newest_copy(ctx: Context, name: str) -> datetime | None:
    saved = backups.list_backups(ctx.paths, name)
    times = [parse_iso(saved[0].meta["time"])] if saved and "time" in saved[0].meta else []
    snapshots = [
        e for e in history.read(ctx.paths, name) if e.action == "snapshot" and e.result == "ok"
    ]
    times += [parse_iso(snapshots[0].time)] if snapshots else []
    return max(times, default=None)


def check_leftovers(ctx: Context) -> list[Finding]:
    """Data moved aside by restores and rollbacks, waiting for a human to delete it."""
    leftovers = sorted(
        str(path)
        for service in ctx.services([])
        for directory in service.data
        for path in directory.parent.glob(f"{directory.name}.*-restore-*")
    )
    if leftovers:
        return [Finding("leftovers", WARN, "delete when no longer needed: " + ", ".join(leftovers))]
    return [Finding("leftovers", OK, "no set-aside data")]


def check_registry(ctx: Context) -> list[Finding]:
    broken = [
        Finding("registry", FAIL, f"{name}: broken service file: {error}")
        for name, error in ctx.registry.broken.items()
    ]
    return broken + _unregistered(ctx)


def _unregistered(ctx: Context) -> list[Finding]:
    registered = {service.workdir for service in ctx.services([])}
    services_root = Path.home() / "services"
    unregistered = (
        sorted(
            path.name
            for path in services_root.iterdir()
            if path.is_dir() and path not in registered
        )
        if services_root.is_dir()
        else []
    )
    if unregistered:
        return [Finding("registry", WARN, "not managed by srvr: " + ", ".join(unregistered))]
    return [Finding("registry", OK, f"{len(registered)} services registered")]


CHECKS: list[tuple[Callable[[Context], list[Finding]], bool]] = [  # (check, needs sudo)
    (check_units, False),
    (check_disk, False),
    (check_ssd, True),
    (check_memory, False),
    (check_reboot, False),
    (check_apt, False),
    (check_tailscale, False),
    (check_containers, True),
    (check_health, False),
    (check_backups, False),
    (check_leftovers, False),
    (check_registry, False),
]


def run(args: Namespace, ctx: Context) -> int:
    with_sudo = docker_access(ctx, prompt=not args.no_sudo)
    findings: list[Finding] = []
    for check, needs_sudo in CHECKS:
        if needs_sudo and not with_sudo:
            findings.append(
                Finding(check.__name__.removeprefix("check_"), WARN, "skipped (needs sudo)")
            )
            continue
        try:
            findings += check(ctx)
        except Exception as exc:  # one broken check must not hide the others
            findings.append(
                Finding(check.__name__.removeprefix("check_"), FAIL, f"check crashed: {exc}")
            )
    rows = [[f.check, ctx.ui.state(f.level), f.detail] for f in findings]
    ctx.ui.table(["CHECK", "RESULT", "DETAIL"], rows)
    return 1 if any(f.level == FAIL for f in findings) else 0
