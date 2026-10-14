# Security

srvr assumes one trusted administrator on a private Linux Docker Compose host. It invokes privileged commands through sudo; registered projects and their source are trusted code. Review deployments and maintain off-server backups.

Do not expose srvr through an HTTP endpoint or let untrusted users supply registry entries, Compose files, Git sources, or command arguments. Remote automation requires authentication, authorization, input validation, and a privilege boundary designed for that exposure; srvr does not provide them. Private app access uses loopback bindings and a separately configured private network. Non-loopback publication and host networking require separate registered opt-ins; srvr does not configure a firewall or make either safe automatically.

## Supported versions

Before the first release, fixes target current `main`. After release, fixes target the latest released version; older versions should upgrade. Support is best effort, with no guaranteed response time or backports.

## Report privately

Use [GitHub's private vulnerability reporting form](https://github.com/Muatasim-Aswad/srvr/security/advisories/new) with the affected version or commit, impact, and minimal reproduction. Remove credentials, private hostnames, and personal data. Do not post vulnerability details in public issues or pull requests.

If you cannot use GitHub, email [malasoad2021@gmail.com](mailto:malasoad2021@gmail.com).

## Operational limits

Service locks cover deployment validation, preparation, switching, recovery and readiness. Confirmed plans are revalidated before mutation. Direct Docker, Git, and editor commands bypass these locks. Persistent deployment/configuration and restore guards block srvr mutations until recovery succeeds, including raw Compose mutations; read-only inspection remains available. A whole deployment is not crash-atomic. Docker restart policies can start containers after reboot independently of these guards. See [Updates](docs/updates.md) and [Restore recovery](docs/backups.md#local-restore-recovery).

Backups cover registered directories and explicitly registered plain local named volumes on a local Unix-socket Docker daemon. Volume ownership, driver/options and filesystem identities are checked; shared/external container use, mount options, mounted/nested storage and remote storage are refused. External databases, unregistered data and anonymous volumes are outside recovery coverage. Restores retain replaced and failed data without removing Docker volumes. Off-server backups do not capture Docker images.

Configuration snapshots can contain credentials; baselines, deployment records and operation models are private, not encrypted. Summaries hide resolved values, but verbose commands, raw Compose output, logs and offline indexes may expose private information. Configuration staging refuses symlink paths before writing. Readiness checks selected long-running components, Docker health and configured endpoints within a timeout; application-specific correctness still depends on those checks.

The supported setup is one administrator and one host, with Debian/systemd guides; an apt repository and unattended app updates are not provided. See [local data and privacy](docs/services.md#local-data-and-privacy) for retained records and sensitive output.
