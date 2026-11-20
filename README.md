# srvr

An opinionated setup and lightweight CLI for running a private home server.

Follow one documented path from Debian to private apps, encrypted off-server backups, and health alerts, with fewer choices to research and piece together. Use one CLI to inspect, update, back up, and recover your apps, with explicit workflows for people and AI agents. Already running Docker Compose? Register your existing projects and keep using their files.

## Why srvr

- **A setup that fits together:** guides choose Debian, Docker, Tailscale, restic with Backblaze B2, and Healthchecks.io, with configuration, checks, and recovery steps.
- **One operating workflow:** see status, health, versions, logs, and deployment history across your apps.
- **Update with a recovery path:** prepare builds or images, back up registered data, check health, and roll back failed switches.
- **Keep your Compose projects:** select source builds or published images from the same files, with fixed profiles and host settings in env files or external overrides.

## Get started

**New server:** follow [Bootstrap](docs/bootstrap.md), [Backups](docs/backups.md), and [Monitoring](docs/monitoring.md):

Debian → private access → Compose apps → encrypted off-server backups → health and backup alerts.

The guides cover host configuration, app and data layout, scheduled jobs, restore tests, and recovery after losing the host. Each stage includes checks before moving on. You configure the host by following the guides; the CLI handles app operations.

**Existing Compose host:** [install srvr](docs/install.md), prepare your app’s config and data paths, then [register it](docs/services.md):

```sh
srvr add notes
srvr info notes
srvr health notes
```

The wizard registers an existing project. Add `--start --backup` to prepare, start, and take the first local backup of a stateful app that is not already running.

## Updates and recovery

For a registered source app, review changes before deploying:

```sh
srvr update notes --check
srvr update notes
```

srvr prepares the new build while the app serves, then stops the project, backs up registered data, runs any configured migration, starts the new version, and checks health. A failed switch triggers rollback to the previous version and its data backup.

To undo a successful update later, `srvr rollback notes --data` restores the previous version and its pre-update data backup. Published-image apps use explicit versions, for example `srvr update notes --component app --image vendor/notes:1.2.3`. Repeat `--set-image COMPONENT=IMAGE` to deploy related images together. Registered configuration changes use `srvr update notes --config` or staged `--config-file` replacements.

Read [Updates and rollback](docs/updates.md) before your first deployment. Backups cover registered directories and explicitly registered plain local Docker named volumes. External databases, shared storage and anonymous volumes need their own recovery plan. See [operational limits](SECURITY.md#operational-limits).

## Everyday commands

```sh
srvr                         # status of all registered apps
srvr doctor                  # whole-server checks
srvr logs notes -f           # follow an app’s logs
srvr update --check          # review available changes
srvr backup notes            # consistent local data backup
```

`srvr -h` and `srvr <command> -h` show options. The [command reference](docs/commands.md) covers every command and use from scripts or agents.

## Scope and safeguards

srvr supports one trusted administrator managing multiple Compose projects on a private Linux host. You prepare app files and secrets. Source deployments keep their repository’s Compose files authoritative.

The CLI uses Python 3.11+ and its standard library only. Install a Debian package or run from a Git checkout; Docker operations use sudo. Commands provide readable output, explicit options, exit codes, and documented procedures.

Service locks serialize changes. Restores move replaced data aside, and health checks gate deployments. Local backups stay on the host: complete the off-server backup setup to protect against disk loss.

Read [operational limits](SECURITY.md#operational-limits) and [local data and privacy](docs/services.md#local-data-and-privacy) before managing stateful apps.

Example status output with synthetic services:

```text
SERVICE  CONTAINERS  HEALTH  VERSION              LAST CHANGE  URL
notes    healthy     ok      4b73a1c              update ok    https://notes.example.ts.net
tasks    healthy     ok      example/tasks:1.2.3  backup ok    -
```

## Documentation

- [Bootstrap](docs/bootstrap.md) — Set up a private home server.
- [Installation](docs/install.md) — Install the CLI or read its documentation offline.
- [Services](docs/services.md) — Register a Compose app and configure its deployment.
- [Command reference](docs/commands.md) — Look up commands and options for daily use or automation.
- [Updates](docs/updates.md) — Update apps and recover from a failed deployment.
- [Backups](docs/backups.md) — Set up off-server backups and restore app data.
- [Security](SECURITY.md) — Understand the supported trust boundary and report vulnerabilities.
- [Development](docs/development.md) — Find where the code lives, run checks, and get started contributing.
- [Release notes](docs/release-notes.md) — See what changed in each release.

Licensed under [MIT](LICENSE).
