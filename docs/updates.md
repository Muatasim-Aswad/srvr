# Updates and rollback

[README](../README.md) · [Commands](commands.md) · [Services](services.md) · **Updates**

Choose a version, prepare it while the current app serves, then switch with a data backup and health checks. Failed switch steps trigger rollback.

**Jump to:** [Update flow](#how-update-works) · [Published images](#published-images) · [Image recovery](#image-pins-and-host-recovery) · [Rollback](#rollback-and-preparation-failures) · [Build stamp](#build-stamp).

## How `update` works

1. **Plan.** Git services fetch the registered branch (or `--ref`) and list the new commits; only fast-forwards are accepted without `--ref`. Image services require an explicit `--image name:tag[@sha256:…]`; `latest` and untagged references are refused.
2. **Prepare.** Move the checkout (or edit the image line) and build (or pull) while the current version keeps serving. A failure here reverts the checkout or pin. Git builds get the build stamp (below).
3. **Switch.** Record the running images (tagged `srvr-prev/<project>-<component>`), stop, back up the data, run the `migrate` service if registered, start, and wait for health.
4. **Roll back automatically** if any switch step fails: restore that backup (the failed run's data is kept aside), previous code/pin and images, then start.

## Concurrency and recovery

Mutating commands hold a nonblocking service lock through readiness checks; a competing command fails with “another srvr command” instead of waiting. Prune locks every registered service before deleting anything. Registrations sharing a project, checkout or overlapping data paths are rejected to prevent separate service names bypassing the lock. Direct Docker/Git/editor commands do not participate in these locks.

After confirmation, updates recheck the checkout and registered configuration; rollback rechecks the deployment record and current version. A stale plan is refused: review and rerun it. Deployment records, runtime pins and image-file edits use atomic replacement. These safeguards do not make a whole deployment crash-atomic: after a killed update, inspect history, containers and the deployment record before recovering.

Data restore has its own persistent recovery journal; see [Backups](backups.md#local-restore-recovery).

## Published images

An image stack with several components needs `--component <name>`, or `update_component` in its registry file. A single image component is selected automatically. Only that component's literal `image:` line in the first Compose file changes, and only its image is pulled. Environment-supplied or overridden image values are refused; the resolved Compose model must change only the selected image. Other running components keep their captured image IDs, although the stack still stops for a consistent backup. For example:

```sh
srvr update tasks --component app --image example/tasks:1.3.0
```

Git updates use the repository's Compose files unchanged. `--component` and `update_component` apply only to image deployments.

## Image pins and host recovery

Image pins live in a state-owned `images.compose.json` override. Rollback uses captured image IDs with pulling disabled, including for digest-pinned or changed tags; subsequent start/restart/Compose commands keep those pins. A later update replaces the relevant pins; a Git update clears them after preparing the new version. Builds, pulls and update planning use the registered base configuration. `srvr compose <svc> config` shows the effective configuration including runtime pins. The offline index includes the override when present.

Off-server backups do not include Docker images. On a rebuilt host, prepare the registered images first (`srvr compose <svc> pull` and, for source apps, a build), then remove any restored `images.compose.json` whose captured images are unavailable before starting. Git checkouts and image versions must match the restored data.

For data recovery, see [Backups](backups.md#4-test-a-restore).

## Multiple services

With several services (or `--all`), Git services go first and image services without `--image` are listed as pinned and skipped; naming a single image service without `--image` is an error.

## Rollback and preparation failures

`srvr rollback` later undoes the last successful update; `--data` also restores the backup taken during it.

If preparation fails, srvr restores the current configuration and keeps the previous deployment record, backup link and rollback image tags. The failed attempt is still recorded in history.

Current images are protected as `srvr-preparing/<project>-<component>:latest` before a build or pull can replace their tags. Only after preparation succeeds are `srvr-prev` tags replaced. Both namespaces keep at most one image reference per project component.

## Build stamp

Every Git build srvr runs (`add --start`, `update`, and a rollback that rebuilds) exports, through `sudo env`:

| Variable | Value |
|---|---|
| `APP_REVISION` | Deployed commit SHA |
| `APP_COMMIT_TIME` | Its commit time, UTC ISO 8601 |
| `APP_BUILD_TIME` | Build time, UTC ISO 8601 |

Apps opt in by passing them as Compose `build.args` (`APP_REVISION: ${APP_REVISION:-}`) to Dockerfile `ARG`s; others ignore them. This lets an image report its commit even when `.git` is excluded from the build context. `srvr compose <svc> build` does not inject a stamp; the app may then report `dev`.

A Git service may register `version_url`, its loopback `version.json`; `srvr health` then fails unless that file's `revision` is the checkout's commit, catching a deploy that kept serving an old build.
