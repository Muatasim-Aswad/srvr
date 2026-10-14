# Updates and rollback

[README](../README.md) · [Commands](commands.md) · [Services](services.md) · **Updates**

Choose a version, prepare it while the current app serves, then switch with a data backup and health checks. Failed switch steps trigger rollback.

**Jump to:** [Update flow](#how-update-works) · [Published images](#published-images) · [Image recovery](#image-pins-and-host-recovery) · [Rollback](#rollback-and-preparation-failures) · [Build stamp](#build-stamp).

## How `update` works

1. **Plan.** Git services fetch the registered branch or selected `--ref`; only fast-forwards are accepted without `--ref`. An explicit branch, tag or supported commit ID does not require an unused registered branch to exist. Fetch errors fail instead of using stale remote refs. Image services require explicit versions through `--image` or `--set-image`; `latest` and untagged references are refused.
2. **Prepare.** A private staged project builds or pulls all needed images while the current checkout, configuration and containers remain in place. Git builds receive the build stamp below. Preparation failure preserves the serving installation and previous rollback record.
3. **Switch.** Capture current image IDs (retained as `srvr-prev/<project>-<component>`), publish a recovery guard, stop, take one registered-data backup, activate the candidate, run migrations, start using prepared image IDs and wait for readiness. Migration/startup/rollback disable implicit builds and pulls.
4. **Roll back automatically** after a handled switch failure: stop, restore the backup if taken, previous code/configuration and captured images, then check readiness. Replaced and failed data remain aside.

`kind = "git"` builds services declaring `build` and pulls image-only dependencies, including prebuilt-only Git stacks. `kind = "image"` pulls every required published component even when it also declares `build`. The same Compose configuration supports either mode without extra user-maintained deployment files. Image mode ignores irrelevant Dockerfiles/build paths, but Compose must still resolve the registered configuration. Preparation failures never fall back to the other mode. See [service selection](services.md#validation).

## Concurrency and recovery

Mutating commands hold a nonblocking service lock through validation, preparation, switching, recovery and readiness; a competing command fails with “another srvr command” instead of waiting. Prune locks every registered service before deleting anything. Registrations sharing a project, checkout, overlapping data paths or named volumes are rejected. Direct Docker/Git/editor commands do not participate in these locks.

After confirmation and preparation, updates recheck the checkout, selected ref, registered files, deployment record and pins; rollback rechecks its record/current files before mutation. A stale plan is refused: review and rerun it. Configuration replacement preserves target permissions, ownership and modification times and journals partial replacements; concurrent edits prevent automatic overwrite. Symlink configuration paths are refused during staging.

An incomplete deployment/configuration recovery blocks srvr mutations, including raw Compose; inspect read-only status/configuration and recover with `srvr rollback SERVICE`, adding `--data` when required. Keep the records/journals and captured images. These safeguards do not make a whole deployment crash-atomic: after a killed update, inspect history, containers and records before recovering.

Data restore has its own persistent recovery journal; see [Backups](backups.md#local-restore-recovery).

## Published images

An image stack with several components needs `--component <name>`, `update_component` in its registry file, or explicit batch selections. A single image component is selected automatically. For example:

```sh
srvr update tasks --component app --image example/tasks:1.3.0
srvr update tasks --set-image app=example/tasks:1.3.0 \
  --set-image worker=example/tasks-worker:1.3.0
```

Selected literal image references are edited narrowly. Environment/shared/overridden expressions use a private state-owned `versions.compose.json` override instead of rewriting those expressions. The resolved target must change only the selected images. Only selected or missing required components are prepared; unselected running components retain their captured image IDs. A batch uses one consistent backup and one rollback record. The registered migration can be explicitly selected even when its tools profile is inactive.

Git updates use the repository's Compose files unchanged. `--component`, `update_component` and `--set-image` apply only to image deployments. Summaries hide env-derived resolved values; raw Compose output may contain secrets.

## Configuration deployments

Capture a healthy installation's known-good baseline before editing registered files, unless a successful deployment already created it:

```sh
srvr update tasks --capture-config
srvr update tasks --config-file /home/admin/.config/tasks/config.env=/home/admin/tasks-next.env
```

Repeat `--config-file REGISTERED=CANDIDATE` for coordinated replacements. The target must be an exact absolute registered path; the candidate supplies content while target metadata is preserved. Alternatively, `srvr update tasks --config` deploys already edited registered files. Add `--check` to review either configuration plan. Version-selection flags cannot be combined with configuration deployment.

The baseline privately covers registered Compose/env/config files and the implicit project `.env`; register other files needed by the app. Candidate images/configuration are validated and prepared before stopping. Handled failures restore the known-good configuration, captured images and the pre-switch data backup; a successful deployment replaces the baseline. Manual rollback also restores captured configuration. Existing baselines cannot be replaced by `--capture-config`; changed registration/path identities require administrator review. Deploy Git-tracked configuration through `--ref`, keeping host settings in untracked env files or external overrides.

`srvr start` still applies current files directly; use the configuration deployment route for transactional changes. Configuration snapshots contain secrets and remain in private state, outside Git.

## Migrations and readiness

An app may migrate during its normal startup. A registered `migrate` component, or a selected dependency using `condition: service_completed_successfully`, runs as a one-off before its dependants. Declare prerequisites with Compose `depends_on`; srvr starts them and waits for running/healthy state before the job, then starts the remaining app components. Required dependencies outside the registered profiles and dependency cycles are refused. Rollback does not rerun migration jobs.

Data backups precede explicit and automatic startup migrations. `--no-backup` is refused for declared migration jobs when data is registered; use the default backup for automatic app migrations too. Only registered local data is recoverable: external databases and unregistered/shared data need an independent recovery plan.

Readiness requires every selected long-running component and expected replica to exist and run without failed health, plus configured HTTP/source-revision checks. One-off migration containers and inactive profiles do not satisfy it. Docker health `starting` state, including start periods, remains pending within `health_timeout` (default 90 seconds); success requires two observations. Each one-off job is bounded by `migration_timeout` (default 600 seconds) and removed on timeout/interruption before recovery. These are startup checks, not ongoing supervision.

## Image pins and host recovery

Exact prepared/captured image IDs live in a state-owned `images.compose.json` override, separate from selected versions in `versions.compose.json`. Rollback uses captured IDs with builds/pulls disabled, including digest-pinned or changed tags. Subsequent start/restart/Compose commands keep runtime pins; a later update replaces them with its prepared IDs. Builds, pulls and update planning use registered configuration plus the owned version override, without runtime pins. `srvr compose <svc> config` shows effective configuration including runtime pins. The offline index includes both overrides when present.

Off-server backups do not include Docker images. On a rebuilt host, prepare the registered images first (`srvr compose <svc> pull` and, for source apps, a build), then remove any restored `images.compose.json` whose captured images are unavailable before starting. Git checkouts and image versions must match the restored data.

For data recovery, see [Backups](backups.md#4-test-a-restore).

## Multiple services

With several services (or `--all`), Git services go first and image services without `--image` are listed as pinned and skipped; naming a single image service without `--image` is an error.

## Rollback and preparation failures

`srvr rollback` later undoes the last successful update; `--data` also restores the backup taken during it.

If preparation fails, srvr restores the current configuration and keeps the previous deployment record, backup link and rollback image tags. The failed attempt is still recorded in history.

Current images are protected as `srvr-preparing/<project>-<component>:latest` before a build or pull can replace their tags; prepared images are retained as `srvr-ready/<project>-<component>:latest`. Only after preparation succeeds are `srvr-prev` tags replaced. Each namespace keeps one reference per project component; do not remove images needed by pending recovery.

## Build stamp

Every Git build srvr runs (`add --start`, `update`, and a rollback that rebuilds) exports, through `sudo env`:

| Variable | Value |
|---|---|
| `APP_REVISION` | Deployed commit SHA |
| `APP_COMMIT_TIME` | Its commit time, UTC ISO 8601 |
| `APP_BUILD_TIME` | Build time, UTC ISO 8601 |

Apps opt in by passing them as Compose `build.args` (`APP_REVISION: ${APP_REVISION:-}`) to Dockerfile `ARG`s; others ignore them. This lets an image report its commit even when `.git` is excluded from the build context. `srvr compose <svc> build` does not inject a stamp; the app may then report `dev`.

A Git service may register `version_url`, its loopback `version.json`; `srvr health` then fails unless that file's `revision` is the checkout's commit, catching a deploy that kept serving an old build.
