# Register and configure services

[README](../README.md) · [Commands](commands.md) · **Services** · [Updates](updates.md)

Prepare an app using [Bootstrap](bootstrap.md#5-add-your-first-app), then register it here. A srvr service is one Compose project; a component is a container service within that project, such as `app` or `db`.

## Add a service

Prepare an existing source checkout or published-image Compose project, its configuration, and its data directories. Then run `srvr add <name>` for guided registration. Relative input paths resolve against the project directory. The wizard suggests existing writable bind-mounted directories for backups; review them to include all persistent data. It shows numeric data ownership and permissions without changing them. Config file contents and resolved Compose secrets are never displayed.

For scripts, pass the options and `-y`:

```sh
srvr add notes --kind git --workdir ~/services/notes --branch main \
  --env-file ~/.config/notes/config.env --data ~/.local/share/notes \
  --migrate migrate --health http://127.0.0.1:8080/health \
  --url https://notes.example.ts.net -y

srvr add tasks --kind image --workdir ~/services/tasks \
  --data-volume tasks_database --profile worker --update-component app -y
```

Registration creates srvr's private registry and lock files. Prepare project files, data directories, secrets, repository access, and Tailscale separately. Source checkouts must be clean and contain the registered branch. Host settings belong in env files; pass repeated `--compose-file` options for external overrides only when needed. Existing registrations, projects and workdirs cannot be registered again under another name.

### Validation

`kind = "git"` builds components declaring `build` and pulls image-only dependencies. `kind = "image"` pulls published components even if they also declare `build`; required components need explicit non-`latest` tags or digests. Failed preparation never falls back to the other mode. The same Compose files can serve either mode; image mode needs no Dockerfile/source build inputs when Compose can resolve the configuration. Git updates keep repository-owned files authoritative.

Published ports bind to `127.0.0.1` or `::1` by default. `--allow-public-ports` registers `allow_public_ports = true` for deliberately configured broader bindings. `--allow-host-network` separately registers `allow_host_network = true`; host networking bypasses published-port restrictions. Neither flag changes Compose files, firewalls or host settings.

Repeat `--profile NAME` to fix the active profiles in `profiles = [...]`. Unprofiled components always participate; ambient `COMPOSE_PROFILES` is refused, including in interpolation env files. All profiles undergo network/image validation, but only the registered selection starts and contributes to readiness. The registered `migrate` job may run even in an inactive tools profile. Raw Compose cannot change project/files/env/profiles or publish ad hoc `run` ports.

Repeat `--data-volume NAME` to register actual resolved Docker volume names in `data_volumes = [...]`, such as `tasks_database`, not logical Compose keys or Docker mountpoint paths. Create volumes before backup or initial startup that needs a data backup. Only plain local volumes with exclusive registered/project ownership are supported; see [coverage and recovery](backups.md#1-check-coverage). Unregistered named/anonymous volumes and writable bind mounts outside registered directories produce coverage warnings. Registrations cannot share projects, workdirs, overlapping directories or volume names.

### Optional startup and backup

`--start` prepares the selected mode's exact images, backs up registered data before startup/migrations, readies migration prerequisites, starts the project and checks health. It refuses a project with running containers; use `update` to redeploy an existing installation. `--backup` explicitly takes a local data backup after any requested startup. Failed startup restores its data backup, leaves the project stopped and keeps the registration for inspection and retry. Existing registrations default to no profiles/volumes and no network opt-ins; no registry migration is required.

Private Git access uses a per-repository read-only deploy key. Tailscale Service creation and host approval remain manual; preserve existing Serve path handlers. Registered data joins `srvr snapshot --all` only when an off-server backup job is separately configured. See `srvr add -h` for all options.

## Files

| Path | Contents |
|---|---|
| `/usr/lib/srvr/` or `~/.local/lib/srvr/` | Packaged code or source checkout; see [Installation](install.md) |
| `~/.config/srvr/services/<name>.toml` | Service registry; see [service.toml](../examples/service.toml) |
| `~/.config/srvr/srvr.toml` | Optional global settings, below |
| `~/.local/state/srvr/<name>/` | History, deployment record, configuration baseline/recovery journal, optional `versions.compose.json` and `images.compose.json`, lock |
| `~/.local/share/srvr/backups/<name>/` | `<UTC time>-<reason>.tar.zst` with a `.json` sidecar (checksum, version, paths) |

## Settings

Every key in `~/.config/srvr/srvr.toml` is optional. Defaults:

```toml
keep_backups = 5
health_timeout = 90           # seconds
migration_timeout = 600       # seconds per one-off job
backup_max_age_days = 7
disk_warn_percent = 85
reboot_fail_days = 3
selftest_max_age_days = 8
```

srvr honors the XDG config, state, and data directory variables; the table shows their default locations.

## Local data and privacy

srvr has no telemetry or account service. It stores registry paths/URLs, deployment records, captured configuration, event history, snapshots, and backups locally. Captured configuration and app data can include personal information and credentials; local files, archives and snapshots are not encrypted. The registry and state directories are owner-only. Keep app secrets out of shared output: container logs, verbose commands, raw Compose config, event notes, and the offline index can reveal private details.

Commands contact the configured Git repositories, image registries, and health endpoints as needed. The optional backup/monitoring recipes send encrypted backups to your chosen storage and diagnostic output to Healthchecks; review and minimize that output before enabling them. Those providers receive connection metadata and use their own retention policies.

`keep_backups` bounds ordinary local backup retention; the backup needed by rollback remains protected. `prune` removes eligible surplus backups. Deployment records, restore journals, image pins, and event history remain until replaced or explicitly maintained. To clear only history, stop scheduled srvr jobs, wait for commands to finish, and truncate the service's `history.jsonl`; do not remove its directory, lock, deployment record, pins, or recovery journal. Snapshots, replaced data, off-server copies, and provider logs need separate retention/deletion. Keep recovery data until a restore has been verified; package removal preserves user data.
