# Development and testing

[README](../README.md) · [Updates](updates.md)

## Code layout

- `srvr/cli.py` discovers every public module in `srvr/commands/`; each module is one command (`HELP`, `configure(parser)`, `run(args, ctx) -> int`). Add a command by adding a file.
- `srvr/commands/_shared.py` holds argument and display helpers used by several commands.
- `srvr/core/` holds shared domain/system operations: registry/configuration, Shell (all subprocesses), Docker/Compose/Git, health, backup/snapshot/volume restoration, deployment planning/preparation/transactions, configuration snapshots, startup/migrations, history and locks.

## Workflow

Edit, test and commit on the administration computer. Once reviewed changes are available in the remote repository, update the server’s read-only checkout:

```sh
cd ~/.local/lib/srvr && git pull --ff-only
```

The server checkout never commits or pushes.

Package installations instead upgrade by installing a newer `.deb`; see [Installation](install.md).

## Development

Install [mise](https://mise.jdx.dev/getting-started.html), then use the same gates as CI:

```sh
mise trust
mise install
mise run install-hooks
mise run verify
```

`mise run verify` checks lint, formatting, types, docs, workflow syntax, and unit/regression tests. Run `mise run verify-all` when changing deployment, recovery, or packaging; it also exercises real Docker deployments and the Debian package lifecycle.

[`mise.toml`](../mise.toml) pins tools and defines task arguments; `uv.lock` locks development dependencies. Runtime dependencies stay in the standard library. Hooks validate the staged files before commit and outgoing commits before push, using temporary snapshots so unstaged edits cannot hide a failure. Hook installation refuses to replace a different existing hooks directory.

CI runs the default gate on Python 3.11, 3.13 and 3.14, plus separate Docker, Debian, and push/PR-range secret-scan jobs. The aggregate **Required checks** job fails if any job fails; use that check in branch protection. The default gate scans an allowlisted source snapshot with redacted gitleaks output. Package artifacts are retained for review. Actions use commit pins and read-only permissions except the release publishing job; Dependabot groups weekly updates by ecosystem with a three-day version cooldown. Security updates remain immediate. CI never deploys a server.

For a focused check:

```sh
python3 -m unittest
mise run verify -- --docker-only
mise run verify -- --package-only
```

Read the repository's `AGENTS.md` for contribution and safety rules. Regression tests cover failed directory switches, extraction errors, interruption/retry recovery, snapshot replacement, stale confirmations, lock contention and protected rollback backups.

Acceptance tests require a working local Docker daemon, Git, GNU tar, zstd, GNU coreutils/findutils, util-linux and the cached images `alpine:latest`, `python:3.14.6-slim-trixie` and `registry:3`. They create isolated projects, repositories, loopback ports, directories and volumes, then remove only owned fixture resources and image tags. Builds and image pushes/pulls are real; sudo authentication is adapted. Ordinary app fixtures use the test user's UID/GID; disposable root helpers test volume ownership/mode preservation. The suite does not test live-host sudo authentication.

The 21 Docker scenarios cover source/prebuilt/mixed stacks, same-Compose source/image modes, env/override versions, coordinated images and preparation failures, configuration rollback, committed explicit/automatic migrations, timeouts, selected-component readiness, named-volume archive/snapshot/restore and interruption guards, fixed profiles and network opt-ins. Assertions check served content, data, exact image IDs, metadata, clean checkouts and owned registry requests. Default regressions also use real temporary Git remotes for deleted-default explicit refs and real filesystem paths for candidate symlink refusal. Older Engine/Compose versions and general power-loss recovery remain outside this gate.

To select compatibility tools without changing the system installation, set `SRVR_TEST_PYTHON` to a Python executable and `SRVR_TEST_COMPOSE` to a standalone Compose binary. The tests invoke srvr through those tools. Compatibility checks use Python 3.13 and Compose 2.26.1 on the local Docker daemon; this does not reproduce the server's Docker Engine version. `SRVR_TEST_BASE_IMAGE` and `SRVR_TEST_REGISTRY_IMAGE` can select pre-cached fixture images.

## Build and test the Debian package

On a development machine with Docker access:

```sh
mise run verify -- --package-only
```

The builder copies only `packaging/source-files.txt` entries into a temporary source snapshot, installs build tools in a digest-pinned Debian 13 container using a fixed Debian package snapshot, runs unit tests, generates offline HTML/Markdown and `srvr.1`, builds with debhelper, and checks lintian and exact package contents. Artifacts and `SHA256SUMS` go in `dist/`. Builds run as your UID/GID; they do not install anything on the host. Refresh the base digest and Debian snapshot together when updating build tools.

The package test installs and upgrades srvr in a disposable container, exercises every command's help and redacted settings as an ordinary user, checks offline links and the man page, then removes/purges the package and verifies user data survives. It enables srvr's docs in the slim image's documentation filter and does not start Docker workloads.

On Debian, the native build needs `build-essential`, `dpkg-dev`, `debhelper`, `dh-python`, `python3`, and `pandoc`; run `dpkg-buildpackage --build=binary --no-sign`. To build docs alone, run `python3 scripts/build_docs.py`; output goes in `.build/`.

`pyproject.toml` is the product version source; the runtime reads it, and the Debian changelog records packaging history. Update its current entry when bumping the product version. `packaging/source-files.txt` is the exact public source inventory: update it when adding/removing tracked files. Generated docs and `srvr/build.json` stay out of Git.

## Release

Rehearse a committed version without publishing:

```sh
mise run release-check
```

The [release driver](../scripts/release.py) defaults to `HEAD` and `dist/release`; use `--ref` or `--output` after `--` to select another candidate or output directory. The rehearsal exports the ref with `git archive`, scans it, runs the full gate, builds the package, checks exact source/package contents, smoke-tests installation, and writes source, release notes, provenance, and checksums. The commit timestamp sets `SOURCE_DATE_EPOCH` and the recorded build time. Use a new output directory for each candidate. Release notes describe behavior, validation, and scope; do not publish private history or local build output.

When release publication is authorized, tag the reviewed commit `vX.Y.Z` and push that tag. The release workflow requires an exact version match, repeats the rehearsal from the tag, downloads and tests the CI artifacts, then uploads the complete bundle to a draft and publishes it. Only that final job receives write permission. Enable immutable releases before tagging. Recover failed publication by rerunning the workflow for the existing tag; do not move the tag or rebuild from `main`. An apt repository and a srvr container image are not provided.

This is an upstream release package, not an upload to Debian's official archive. Its lintian override covers only the expected initial-upload notice about not closing a Debian packaging request.
