# Bootstrap a home server

Set up one Debian server with private apps, backups, and alerts. srvr manages existing Compose projects; you prepare the host and app files.

**Bootstrap** · [Backups](backups.md) · [Monitoring](monitoring.md)

**Start here:** a fresh Debian 13 installation, an admin account with sudo, a second computer, and physical access. Disk encryption is recommended; with manual LUKS unlocking, someone must be there after every reboot.

**Route:** [Debian](#1-prepare-debian) → [Access](#2-set-up-private-access) → [Docker](#3-install-docker) → [srvr](#4-install-srvr) → [First app](#5-add-your-first-app) → [Protection](#6-add-backups-and-alerts).

Run commands on the server as your admin user unless stated otherwise. Replace `admin`, repository URLs, the sample LAN range, and app names with your own values.

## 1. Prepare Debian

Use a minimal installation with SSH and an ordinary admin account. Prefer Ethernet, reserve its address in your router, and leave router port forwarding off.

```sh
sudo apt update
sudo apt upgrade
sudo apt install python3 git curl ca-certificates zstd openssh-server \
  ufw unattended-upgrades apt-listchanges smartmontools
sudo dpkg-reconfigure unattended-upgrades
```

Enable automatic security updates. Set `Unattended-Upgrade::Automatic-Reboot "false";` in `/etc/apt/apt.conf.d/52srvr-reboots`; plan manual reboots when physical unlocking is needed. See [Debian's unattended-upgrades guide](https://wiki.debian.org/UnattendedUpgrades).

For a repurposed laptop, set `HandleLidSwitch=ignore`, `HandleLidSwitchExternalPower=ignore`, `HandleLidSwitchDocked=ignore`, and `IdleAction=ignore` under `[Login]` in `/etc/systemd/logind.conf.d/20-server.conf`. Create the directory if needed, then reboot locally to apply it. Prevent automatic sleep:

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

**Check:** `python3 --version` is 3.11 or newer; `sudo -v` works; `systemctl --failed` has no unexplained failures.

## 2. Set up private access

Install Tailscale using its [Debian package instructions](https://tailscale.com/docs/install/linux). In the admin console, define `tag:server` with an administrator as its owner. Authenticate the server:

```sh
sudo tailscale up --ssh --advertise-tags=tag:server
```

Use a narrow [Tailscale SSH policy](https://tailscale.com/docs/features/tailscale-ssh): allow your admin identity to reach this server as the local admin user. Connect from your second computer and verify the user and sudo access.

Keep key-only OpenSSH on the LAN as a recovery route. Install your client's public key in the admin user's `~/.ssh/authorized_keys` and test a new LAN connection before disabling password access. Put this in `/etc/ssh/sshd_config.d/00-srvr.conf`:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers admin
X11Forwarding no
```

Keep the working session open while checking and reloading:

```sh
sudo sshd -t
sudo systemctl reload ssh
```

Replace `192.168.1.0/24` with your home LAN before enabling the firewall:

```sh
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw allow 41641/udp
sudo ufw enable
```

**Check:** new connections work through both Tailscale and key-only LAN SSH. Only intended users can access the server.

## 3. Install Docker

Follow [Docker's Debian apt repository instructions](https://docs.docker.com/engine/install/debian/#install-using-the-apt-repository), including Engine, Buildx, and the Compose plugin. Keep using sudo for Docker; srvr handles that interactively.

```sh
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

**Check:** the test container succeeds and Compose is available. Publish app ports only on `127.0.0.1` or `::1`; [Docker-published ports can bypass UFW](https://docs.docker.com/engine/install/debian/#firewall-limitations).

## 4. Install srvr

Follow [Installation](install.md) for a Debian package or a Git checkout. No pip environment is needed. Create `~/services` for your app projects.

**Check:** `srvr -h` works from a new login. `srvr doctor` can authenticate sudo and reports the host's condition; review warnings before adding apps.

## 5. Add your first app

Choose an app with documented Compose deployment and persistent-data requirements. Check `ss -ltn` for a free loopback port.

| Put this… | Here |
|---|---|
| Source checkout or pinned-image Compose project | `~/services/notes/` |
| Host settings and secrets | `~/.config/notes/config.env` (directory `0700`, file `0600`) |
| Persistent data | `~/.local/share/notes/` (owned by the container's UID/GID) |

Prepare these files and directories first. Bind-mount every persistent directory, including uploads and attachments; srvr backups only include registered `data` paths. Set `restart: unless-stopped` for long-running components so they return after a host reboot.

- **Source app:** clone the deployment branch and keep the checkout clean. Supply host settings through env variables; use an external Compose override only when necessary. Register its migration component if it has one.
- **Image app:** use an explicit release tag, optionally with a digest. For an app + database stack, choose the app as `update_component` and manage database upgrades deliberately.

Run the wizard for the prepared project:

```sh
srvr add notes --start --backup
srvr info notes
srvr health notes
```

Provide a loopback health URL if the app has one. For a stamped source app, also provide `version_url`; see [Build stamp](updates.md#build-stamp). `--backup` needs registered data; omit it for a stateless app. Already-running projects can be registered without `--start`.

For private HTTPS, follow [Tailscale Services](https://tailscale.com/docs/features/tailscale-services):

1. Define `notes` in the admin console with endpoint `tcp:443`; enable tailnet HTTPS if prompted.
2. On the tagged server, run `sudo tailscale serve --service=svc:notes --https=443 127.0.0.1:8080`, substituting your app's port.
3. Manually approve this host and grant intended users access to the service.
4. Open the resulting private URL from another tailnet device. Add it as `url` in `~/.config/srvr/services/notes.toml`.

Keep Funnel off. When adding to a shared service, preserve its existing path handlers.

**Check:** loopback health and private HTTPS work; `srvr backups notes` shows a local backup for a stateful app.

## 6. Add backups and alerts

1. Follow [Backups](backups.md) for nightly off-server copies. Restore one app into an isolated test location before relying on them.
2. Follow [Monitoring](monitoring.md) for server health and backup alerts. Test both failure and recovery delivery.
3. Save an offline service index: `srvr info --all --markdown > ~/SERVER.md`. Keep a copy on your admin computer, with your recovery instructions.

**Done when:** after an on-site reboot and disk unlock, networking, containers, private HTTPS, backup scheduling, and monitoring return without a login.

## Day to day

```sh
srvr status
srvr doctor
srvr update --check
```

Review updates before applying them. Source apps use `srvr update notes`; image apps require a chosen version, for example `srvr update notes --component app --image vendor/notes:1.2.3`. The [update guide](updates.md#how-update-works) explains rollback and data recovery.
