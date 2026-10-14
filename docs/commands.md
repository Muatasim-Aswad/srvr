# Command reference

[README](../README.md) · [Commands](commands.md) · [Services](services.md) · [Updates](updates.md)

Run srvr as your admin user; it uses sudo for Docker and protected data. Python 3.11+, Docker Compose, GNU tar, and zstd are required; source deployments also use Git. Named-volume recovery also uses GNU coreutils/findutils and util-linux. The supported setup uses Debian, systemd, and Tailscale.

## Commands

| Command | Purpose |
|---|---|
| `srvr add <svc> [options] [-y] [--start] [--backup]` | Guided registration of an existing Compose project; validate configuration, optionally prepare/start and take the first backup |
| `srvr` / `srvr status [svc…]` | One line per service: containers, health, version, last change, URL |
| `srvr info <svc>` / `--all` | Paths, URLs, version, backups, rollback state |
| `srvr info --all --markdown` | Offline index and emergency Compose commands, for the admin record |
| `srvr health [svc…]` | Check selected containers, loopback/private HTTPS endpoints and `version_url` against the checkout; exit 1 on failure |
| `srvr logs <svc> [component] [-f] [-n N] [--since T]` | Container logs |
| `srvr compose <svc> <args…>` | Raw Compose with registered project/files/env/profiles and pins; refuses `down -v`, selection overrides and ad hoc `run` publication |
| `srvr start\|stop\|restart <svc…>\|--all` | `start` also applies changed Compose or env files |
| `srvr update <svc> [--check] [--ref R\|--image I] [--component C] [--force] [--no-backup] [-y]` | Deploy a new version |
| `srvr update <svc> --set-image C=I [--set-image C=I…] [--check] [-y]` | Deploy related image components together, with one backup/rollback record |
| `srvr update <svc> --capture-config [-y]` | Capture a healthy known-good configuration baseline before editing |
| `srvr update <svc> --config\|--config-file REGISTERED=CANDIDATE [--check] [-y]` | Deploy registered configuration transactionally; repeat `--config-file` for replacements |
| `srvr update --check` | What would change, for every service |
| `srvr rollback <svc> [--data] [-y]` | Return to the version before the last update |
| `srvr backup <svc…>\|--all` | Consistent data backup (service stopped for seconds) |
| `srvr snapshot <svc…>\|--all --to DIR` | Plain data copies in `DIR/<service>/` for the off-server backup (service stopped for seconds); a failed copy keeps the previous one |
| `srvr backups <svc>` | List backups |
| `srvr restore <svc> [name\|latest] [-y]` | Put a backup back; current data is moved aside |
| `srvr restore <svc> --snapshot DIR [-y]` | Restore a complete service snapshot, including its named-volume manifest |
| `srvr history [svc…] [-n N]` | Recorded updates, rollbacks, restores, backups |
| `srvr config <svc> [--edit]` | Settings with secret values hidden, or edit them |
| `srvr doctor` | Whole-server checks: units, disk, SMART, memory, reboot, apt, Tailscale, containers, endpoints, backups, and leftovers |
| `srvr prune [-y]` | Dangling images, old build cache, surplus backups |

`-v` prints every external command. `-h` works on every command.

Registration supports repeated `--data-volume NAME` and `--profile NAME`, plus separate `--allow-public-ports`/`--allow-host-network` opt-ins. See [Services](services.md). Configuration deployment and image batches target exactly one service; see [Updates](updates.md) for selection, migrations and recovery. Raw Compose commands require the administrator to choose build/pull/start behavior; use `update` for prepared transactional deployments.

`srvr --version` prints the product version. `srvr --build-info` prints version, revision, commit time, and build time as JSON without reading the service registry. Source checkouts report their Git identity, or `dev` when modified; packages carry the verified build identity. Reproducible release builds use the commit timestamp as their build timestamp.

Doctor checks SMART health, sector counts, and self-test results. A pending reboot fails once it reaches `reboot_fail_days`; backup age uses the newest local srvr backup or snapshot per service. See [Monitoring](monitoring.md) for thresholds and external alerts.

## Scripts and AI agents

- Inspect first with `srvr status`, `srvr info <svc>`, `srvr health <svc>`, and `srvr update <svc> --check`.
- Supply explicit service names and options for repeatable operations; see the [registration examples](services.md#add-a-service).
- Use `-y` on commands that support it to skip confirmation after choosing the action. It does not bypass sudo authentication; unattended jobs need privileges configured separately.
- Check exit codes and verify health after changes. `health` and `doctor` return 1 on failure; doctor warnings alone return success.
- Use `srvr config <svc>` for redacted settings. Raw `srvr compose <svc> config` can contain secrets.

For root-run jobs, set `HOME` to the admin user’s home so srvr finds the same registry and state. Files it creates retain the home owner; see the [backup job](backups.md#3-schedule-the-backup).

## Safety rules

- Refuses `down -v`; image updates require a version you choose. Built-in restores keep replaced app data aside; the `config` command masks secret values.
- Restores and data rollbacks retain replaced data under `*.pre-restore-*`, with failed installed data under `*.failed-restore-*` when recovery needs it. Directory leftovers appear in `srvr doctor`; review named-volume leftovers separately without removing the volume or its `_data` root.
- One srvr command at a time may change a service (per-service lock).
- Unfinished restore/deployment recovery blocks mutations, including raw Compose; retain journals, data and captured images until successful recovery.
- A broken service file affects only that service: other commands skip it with a warning and `srvr doctor` reports it.
- Backups stay on this SSD; they are not off-server backups. `srvr snapshot` only prepares data for one.
