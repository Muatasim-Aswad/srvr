# Release notes

## 0.2.0 — Compose deployments and recovery

- Update env-defined, shared and overridden images through a private version override; literal image edits remain narrow.
- Deploy related components with repeated `--set-image COMPONENT=IMAGE`, one backup/rollback record and unchanged unselected running image IDs.
- Prepare Git stacks containing source builds, published dependencies or only prebuilt images before switching.
- Choose source builds (`kind = "git"`) or published pulls (`kind = "image"`) from the same hybrid Compose files, with no fallback to the other mode after failure.
- Archive, snapshot and restore explicitly registered plain local named volumes, including volume-only services; preserve numeric ownership/modes, `_data` identity and replaced data through journaled recovery.
- Deploy registered Compose/env/configuration changes against a private known-good baseline with `--config` or repeated `--config-file`; recover configuration, images and registered data together.
- Order declared migration prerequisites, bound jobs with `migration_timeout`, and require every selected long-running component/replica plus configured endpoints to be ready. Handled explicit/automatic migration failures restore registered data.
- Keep loopback publication by default; broader bindings and host networking require separate explicit registration opt-ins. Fixed registered profiles govern preparation, startup and readiness.
- Honor explicit Git branches/tags/commit IDs after deletion of an unused registered branch; failed fetches cannot silently use stale remote refs and changed selected refs invalidate confirmation. Default deployments retain their registered-branch/fast-forward checks.
- Refuse candidate configuration symlink paths before staging writes and block volume-deleting raw Compose flag forms such as `down --volumes=true`.

### Validation and scope

The candidate gate covers CLI/core regressions, 21 real Docker scenarios, source inventories/secrets, strict typing/lint/docs/workflow checks, actual Debian builds and package install/upgrade/remove/purge with preserved user data. Real temporary Git/filesystem regressions reproduce deleted-default ref selection and candidate path/volume-deletion guards. CI additionally checks Python 3.11, 3.13 and 3.14; release publication must pass the committed-ref rehearsal and mandatory CI.

Existing registrations and directory-only archives remain supported. Named-volume recovery requires exclusive plain local storage on a local Unix-socket daemon; remote/nonlocal/mounted storage, external databases and anonymous/unregistered/shared data need separate recovery plans. Captured configuration/local data are private, not encrypted; off-server backups exclude Docker images. Readiness is bounded startup observation, not ongoing supervision. Direct Docker/Git/editor actions and Docker restart policies bypass srvr guards; whole deployments are not crash-atomic. Local acceptance does not prove older Engine/Compose compatibility, live-host sudo authentication or general power-loss recovery. Read [Updates](updates.md), [Backups](backups.md) and [Security](../SECURITY.md).

The release bundle contains `srvr_0.2.0-1_all.deb`, `srvr-0.2.0-source.tar.gz`, `RELEASE_NOTES.md`, `PROVENANCE.json`, and `SHA256SUMS`; see [Installation](install.md). An apt repository is not provided.

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
