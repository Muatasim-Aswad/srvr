# Contributing

srvr manages existing Docker Compose projects on one private home server. Keep workflows explicit, recovery understandable, and runtime dependencies in Python's standard library.

## Start here

Read [Development](docs/development.md), [Updates](docs/updates.md), and the repository's `AGENTS.md`. Open an issue before substantial features or changes to the supported setup; focused fixes and documentation improvements can go straight to a pull request.

Ask usage questions in [Discussions](https://github.com/Muatasim-Aswad/srvr/discussions). Fork the repository and send a focused pull request to `main`.

Follow the [development setup](docs/development.md#development) for the pinned toolchain, hooks, and shared local/CI gate.

Run `mise run verify-all` for deployment, recovery, or packaging changes. It needs local Docker access and runs disposable deployment and Debian package checks. Use isolated test registries and data; keep development commands away from live services.

## Pull requests

- Explain the problem, resulting behavior, privacy implications, migrations or compatibility changes, and checks run. Say when these implications do not apply.
- Add a regression test for a bug. Exercise real files, Git, or disposable Docker at existing boundaries; inject failures at process/filesystem boundaries.
- Preserve service locks, rollback images, backups, and recovery journals. Update the relevant guide when behavior changes.
- Keep changes focused and runtime dependencies unchanged unless agreed first. Contributions use the repository's MIT license.

Report suspected vulnerabilities through [Security](SECURITY.md), without putting secrets or exploit details in public issues.
