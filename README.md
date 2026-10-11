# srvr

A lightweight CLI and opinionated setup for running a private home server, with clear workflows for humans and AI agents.

Manage one Docker Compose host: inspect apps, deploy deliberate updates, and recover when something goes wrong. The setup guides choose Debian, Docker, Tailscale, off-server backups, and external health alerts.

## Why srvr

- **One workflow:** status, health, logs, configuration, and Compose commands across your apps.
- **Controlled updates:** prepare builds or images, back up registered data, check health, and roll back failed switches.
- **Clear recovery:** local restores, deployment history, and guides for off-server backups.
- **Easy to operate:** readable output, explicit CLI options, exit codes, and documented procedures for people and agents.

Python 3.11+ standard library only. Install a Debian package or run from a Git checkout; Docker operations use sudo.

## Get started

**New server:** follow [Bootstrap](docs/bootstrap.md) from Debian to your first private app, then set up [Backups](docs/backups.md) and [Monitoring](docs/monitoring.md).

**Existing Compose host:** [install srvr](docs/install.md), prepare your app’s config and data paths, then [register it](docs/services.md):

```sh
srvr add notes
srvr info notes
srvr health notes
```

The wizard registers an existing project. Add `--start --backup` to prepare, start, and take the first local backup of a stateful app that is not already running.

## Everyday commands

```sh
srvr                         # status of all registered apps
srvr doctor                  # whole-server checks
srvr logs notes -f           # follow an app’s logs
srvr update --check          # review available changes
srvr backup notes            # consistent local data backup
```

Source apps use `srvr update notes`. Published-image apps need a chosen version, for example `srvr update notes --component app --image vendor/notes:1.2.3`. See [Updates and rollback](docs/updates.md) before your first deployment.

`srvr -h` and `srvr <command> -h` show options. The [command reference](docs/commands.md) covers every command and use from scripts or agents.

## Scope and safeguards

srvr manages existing Compose projects on one host. You prepare app files and secrets; the setup guides cover host configuration and private access. Source deployments keep their repository’s Compose files authoritative.

Service locks serialize changes. Restores move replaced data aside, and health checks gate deployments. Local backups stay on the host: complete the off-server backup setup to protect against disk loss.

Read [operational limits](SECURITY.md#operational-limits) and [local data and privacy](docs/services.md#local-data-and-privacy) before managing stateful apps.

Example status output with synthetic services:

```text
SERVICE  CONTAINERS  HEALTH  VERSION              LAST CHANGE  URL
notes    healthy     ok      4b73a1c              update ok    https://notes.example.ts.net
tasks    healthy     ok      example/tasks:1.2.3  backup ok    -
```

## Guides and reference

| I want to… | Read |
|---|---|
| Set up a private home server | [Bootstrap](docs/bootstrap.md) |
| Install the CLI or read docs offline | [Installation](docs/install.md) |
| Register an app or find its configuration | [Services](docs/services.md) |
| Look up commands or automate operations | [Command reference](docs/commands.md) |
| Understand updates, rollback, or build stamps | [Updates](docs/updates.md) |
| Set up backups or restore data | [Backups](docs/backups.md) |
| Receive health and backup alerts | [Monitoring](docs/monitoring.md) |
| Work on srvr or run its tests | [Development](docs/development.md) |

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Release notes](docs/release-notes.md). Licensed under [MIT](LICENSE).
