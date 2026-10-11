# Release notes

## 0.1.0 — initial release

A lightweight CLI and opinionated setup for one private Docker Compose home server, with explicit workflows for humans and AI agents.

- Inspect status, health, logs, configuration, deployment history, and whole-server checks.
- Register existing Git or published-image Compose projects. Choose image versions explicitly and update one selected component.
- Prepare updates, back up registered data, run migrations, check health, and roll back failed switches using captured images.
- Create local archives and snapshots; stage restores before switching directories, retain replaced data, and recover interrupted restores through a persistent journal.
- Serialize mutations with service locks, reject stale confirmations, and protect the backup needed by data rollback.
- Follow concise Debian, Docker, Tailscale, off-server backup, and monitoring guides.
- Install a Debian package with a man page and offline HTML/Markdown docs, or run from a Git checkout. Runtime uses Python 3.11+ and its standard library.
- Inspect the CLI's version and build identity; release bundles include allowlisted source, provenance, and checksums, built and tested from the release tag.

### Validation and scope

The verification suite covers CLI/core regressions and seven real Docker scenarios, including committed SQLite migration failures, unhealthy deployments, registry pulls, and data rollback. CI checks Python 3.11, 3.13, and 3.14, source inventories and secrets, plus Debian package installation, upgrade, removal, and purge.

This initial release supports a trusted administrator's single host and registered data directories. Named Docker volumes need a separate backup plan. Whole deployments are not crash-atomic; locks and restore guards apply to srvr commands. Sudo authentication and privileged ownership restoration are outside the Docker acceptance suite. Read [Updates](updates.md) and [Backups](backups.md) before operating stateful apps.

The release bundle contains `srvr_0.1.0-1_all.deb`, `srvr-0.1.0-source.tar.gz`, `RELEASE_NOTES.md`, `PROVENANCE.json`, and `SHA256SUMS`; see [Installation](install.md). An apt repository is not provided.
