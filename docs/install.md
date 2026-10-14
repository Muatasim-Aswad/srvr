# Install srvr

[README](../README.md) · [Bootstrap](bootstrap.md) · [Commands](commands.md)

Choose a Debian package for normal use or a Git checkout for development. Both use the same per-user registry, configuration, state, and backups. Docker, Tailscale, and host setup follow [Bootstrap](bootstrap.md).

## Debian package

Download the [v0.2.0 Debian package](https://github.com/Muatasim-Aswad/srvr/releases/download/v0.2.0/srvr_0.2.0-1_all.deb) and [SHA256SUMS](https://github.com/Muatasim-Aswad/srvr/releases/download/v0.2.0/SHA256SUMS), or use `curl` below. From your download directory, verify the checksum before installing:

```sh
curl --fail --location --remote-name https://github.com/Muatasim-Aswad/srvr/releases/download/v0.2.0/srvr_0.2.0-1_all.deb &&
curl --fail --location --remote-name https://github.com/Muatasim-Aswad/srvr/releases/download/v0.2.0/SHA256SUMS &&
sha256sum --ignore-missing --check SHA256SUMS &&
sudo apt install ./srvr_0.2.0-1_all.deb &&
srvr --version &&
srvr -h
```

The checksum command checks the downloaded package and skips bundle files you did not download. [GitHub releases](https://github.com/Muatasim-Aswad/srvr/releases) also include source, release notes, and build provenance. To verify a whole bundle, download every asset and run `sha256sum --check SHA256SUMS`. To build locally, use `scripts/build-deb`; see [Packaging](development.md#build-and-test-the-debian-package).

The package installs the CLI and its declared dependencies. Prepare Docker Engine, Compose, private access, and app files using the bootstrap guide, then run `srvr add <name>`.

| Installed file | Purpose |
|---|---|
| `/usr/bin/srvr` | CLI, linked to its private launcher |
| `/usr/lib/srvr/` | Python code; no pip environment |
| `/usr/share/man/man1/srvr.1.gz` | `man srvr` |
| `/usr/share/doc/srvr/index.html` | Offline guide index, viewable in a browser |
| `/usr/share/doc/srvr/docs/*.md.gz` | Markdown guides for terminal use or agents |

```sh
man srvr
zless /usr/share/doc/srvr/docs/bootstrap.md.gz
zless /usr/share/doc/srvr/docs/backups.md.gz
```

Offline HTML, compressed Markdown, and the man page are built from the repository's docs. They match the installed package version and need no network connection.

To upgrade, install a newer downloaded `.deb` the same way. `srvr update` updates hosted apps; it does not upgrade srvr itself. Removing the package removes the CLI and packaged docs; per-user app data and configuration stay in place.

## Git checkout

Clone the public repository:

```sh
mkdir -p ~/.local/lib ~/.local/bin ~/services
git clone https://github.com/Muatasim-Aswad/srvr.git ~/.local/lib/srvr
ln -s ~/.local/lib/srvr/bin/srvr ~/.local/bin/srvr
```

Ensure `~/.local/bin` is on your PATH. If needed, add `export PATH="$HOME/.local/bin:$PATH"` to `~/.profile` and log in again. No pip installation is needed.

Update a read-only server checkout with:

```sh
git -C ~/.local/lib/srvr pull --ff-only
```

Generate a separate deploy key on the server for each private repository, register only its public half from your admin computer, and pin the identity with Git's `core.sshCommand`.

## Switch from a checkout to the package

1. Install the package and check `/usr/bin/srvr --version`.
2. Move the old `~/.local/bin/srvr` symlink aside, refresh your shell's command cache, and use `type -a srvr` to confirm which executable runs.
3. Change unattended backup/monitoring jobs to use `/usr/bin/srvr`, keeping `HOME` set to your admin home.
4. Verify `srvr status`, `srvr health`, and the next scheduled jobs before removing the old checkout. Remove checkout paths from backup jobs that list them unconditionally; restic exits non-zero when a path is missing.

Your service registry and data paths need no migration.
