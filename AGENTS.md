# Working on srvr

srvr is a Linux CLI for one trusted administrator's Docker Compose host. Keep runtime dependencies in the Python 3.11+ standard library. Read [Development](docs/development.md) and [Updates](docs/updates.md) before changing deployment behavior.

- Keep CLI parsing/display in `srvr/commands`; put shared domain and system operations in `srvr/core`. Inject dependencies through `Context` and run external commands through `Shell` with argument lists.
- Source deployments use repository-owned Compose files. Host settings belong in env files or external overrides. Image updates change only the selected literal image reference.
- Hold the service lock through state validation, mutation, recovery, and readiness checks. Revalidate plans and deployment records after confirmation. Acquire multiple locks in sorted order; acquire the registry lock before service locks.
- Preserve data and captured rollback images. Stage restores before switching directories; retain old data and recovery journals until recovery succeeds. Incomplete restores must prevent startup. Never remove a lock file or bypass a recovery guard.
- Add regression tests at public CLI or existing core boundaries. Use real temporary files/Git and disposable Docker projects; inject failures only at process/filesystem boundaries. Keep tests outside production code.
- Use isolated XDG directories and test project names. Do not run the development checkout against a live service registry, change a live host, prune shared Docker resources, or expose secrets during tests.
- Run `mise run verify`; use `mise run verify-all` after deployment, data-recovery, or packaging changes. Hooks check staged/outgoing commits. Do not bypass failed gates.
- Markdown lives in `docs/`; keep prose concise and unwrapped. Update the authoritative Markdown, then regenerate HTML/man pages through the package build. Generated output stays out of Git.
- Do not publish releases, deploy, or push without user authorization. Keep commits focused and omit agent signatures/co-author trailers.
