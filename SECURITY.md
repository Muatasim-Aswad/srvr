# Security

srvr assumes one trusted administrator on a private Linux Docker Compose host. It invokes privileged commands through sudo; registered projects and their source are trusted code. Review deployments and maintain off-server backups.

Do not expose srvr through an HTTP endpoint or let untrusted users supply registry entries, Compose files, Git sources, or command arguments. Remote automation requires authentication, authorization, input validation, and a privilege boundary designed for that exposure; srvr does not provide them. Private app access uses loopback bindings and a separately configured private network.

## Supported versions

Before the first release, fixes target current `main`. After release, fixes target the latest released version; older versions should upgrade. Support is best effort, with no guaranteed response time or backports.

## Report privately

Email [malasoad2021@gmail.com](mailto:malasoad2021@gmail.com) with the affected version or commit, impact, and minimal reproduction. Remove credentials, private hostnames, and personal data. Do not post vulnerability details in public issues or pull requests.

GitHub's private vulnerability reporting form is not enabled yet. Once enabled on the public repository, this policy will link to it; use email in the meantime.

## Operational limits

Service locks coordinate srvr commands; direct Docker, Git, and editor commands bypass them. Restores journal recoverable directory switches, but a whole deployment is not crash-atomic. Docker restart policies can start containers after reboot independently of srvr's restore guard. See [Updates](docs/updates.md) and [Restore recovery](docs/backups.md#local-restore-recovery).

Backups cover registered directories; named Docker volumes need a separate backup plan. Off-server backups do not capture Docker images. The supported setup is one administrator and one host, with Debian/systemd guides; an apt repository and unattended app updates are not provided. See [local data and privacy](docs/services.md#local-data-and-privacy) for retained records and sensitive output.
