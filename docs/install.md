# Install srvr

[README](../README.md) · [Bootstrap](bootstrap.md) · [Commands](commands.md)

Choose a Debian package for normal use or a Git checkout for development. Both use the same per-user registry, configuration, state, and backups. Docker, Tailscale, and host setup follow [Bootstrap](bootstrap.md).

## Debian package

Download the `.deb` attached to a [GitHub release](https://github.com/Muatasim-Aswad/srvr/releases), then install it from the download directory:

```sh
sudo apt install ./srvr_0.1.0-1_all.deb
srvr --version
srvr -h
```

Release bundles include `SHA256SUMS`, source, release notes, and build provenance. Download the whole bundle and run `sha256sum --check SHA256SUMS` before installing. To build locally, use `scripts/build-deb`; see [Packaging](development.md#build-and-test-the-debian-package).

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

Use the repository URL below. While the repository is private, access requires authentication; a dedicated read-only deploy key uses the SSH URL `git@github.com:Muatasim-Aswad/srvr.git` instead.

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
4. Verify `srvr status`, `srvr health`, and the next scheduled jobs before removing the old checkout.

Your service registry and data paths need no migration.
