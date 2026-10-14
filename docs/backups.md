# Back up and restore

Keep local backups for quick rollback, encrypted off-server backups for a lost host, and a disconnected copy for recovery. Database replication is not a backup.

[Bootstrap](bootstrap.md) · **Backups** · [Monitoring](monitoring.md)

**Route:** [Coverage](#1-check-coverage) → [Storage](#2-prepare-off-server-storage) → [Nightly job](#3-schedule-the-backup) → [Restore](#4-test-a-restore) → [Maintenance](#5-maintain-a-second-copy).

The recipe below uses restic + Backblaze B2, root-run systemd jobs, and the default srvr directories. Replace `/home/admin`, bucket, endpoint, and credentials. Other restic storage backends can use the same data-copy flow.

## 1. Check coverage

```sh
srvr info --all
srvr backup --all
```

For every stateful app, confirm registered `data` directories and `data_volumes` include its database, attachments, and other persistent files. Register actual resolved Docker volume names with `srvr add --data-volume NAME` or `data_volumes = [...]`; volume-only services are supported. Unregistered/anonymous volumes, external databases and shared data are outside this recipe's coverage.

Named-volume recovery supports plain `local` volumes without driver options, through a local Unix-socket Docker daemon on a supported local filesystem. Remote drivers/endpoints, mounted/nested storage and volumes used by another registration/project or external container are refused. srvr resolves mountpoints itself; never register `/var/lib/docker` or volume mountpoints as data directories. Volume identity and exclusive stopped ownership are checked before copying and restoring. The Debian setup supplies GNU coreutils/findutils and util-linux (`findmnt`, `mountpoint`) alongside tar/zstd.

| Include | Why |
|---|---|
| Stopped-state data copies | Consistent service data, including SQLite sidecar files |
| `~/services`, `~/.config` | Compose files, source, secrets, and registry |
| `~/.local/state/srvr`, `~/SERVER.md` | History and recovery index; also include a source install's `~/.local/{lib/srvr,bin}` |
| `/etc` | Host configuration and systemd jobs; add `/var/spool/cron` if you use cron |

Exclude live app data from restic: use the stopped-state copies instead. Docker images and `/var/lib/tailscale` are also excluded; rebuild/pull images and authenticate Tailscale again after a host rebuild.

## 2. Prepare off-server storage

1. Create a private B2 bucket. Use the [S3-compatible backend recommended by restic](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#backblaze-b2).
2. Create a bucket-scoped server key with `listBuckets,listFiles,readFiles,writeFiles`, without `deleteFiles`, using the [B2 API or CLI](https://www.backblaze.com/docs/cloud-storage-application-keys). Keep a separate maintenance key with deletion rights on your admin computer.
3. Set a [lifecycle rule](https://www.backblaze.com/docs/cloud-storage-lifecycle-rules) to delete hidden versions after 30 days. Leave Object Lock off for this recipe; default retention can interfere with restic lock cleanup.
4. Save the repository address, both keys, and a strong restic password in an encrypted vault outside the server. Keep a second recoverable copy of the vault.

Root can read backups using the server's unattended credentials. The restricted key and hidden-version window give you time to recover earlier versions after malicious changes; they do not replace a disconnected copy.

```sh
sudo apt install restic
sudo install -d -m 0700 /etc/srvr-backup /var/backups/srvr /var/cache/restic
sudo install -m 0600 /dev/null /etc/srvr-backup/server.env
sudo install -m 0600 /dev/null /etc/srvr-backup/password
```

Edit the new files with `sudoedit`. Put only the restic password in `password`; use this template for `server.env`:

```sh
SERVER_HOME=/home/admin
SRVR_BIN=/usr/bin/srvr
RESTIC_REPOSITORY=s3:https://s3.YOUR-REGION.backblazeb2.com/YOUR-BUCKET
AWS_ACCESS_KEY_ID=YOUR-SERVER-KEY-ID
AWS_SECRET_ACCESS_KEY=YOUR-SERVER-KEY
RESTIC_PASSWORD_FILE=/etc/srvr-backup/password
RESTIC_CACHE_DIR=/var/cache/restic
```

Set `SRVR_BIN` to the full path printed by `command -v srvr` as your admin user. A source install normally uses `/home/admin/.local/bin/srvr`.

For each script below, create the file with `sudoedit`, then set mode `0700` with `sudo chmod 0700 <path>`. It must remain root-owned. To initialize and inspect the repository without printing credentials, save this as `/usr/local/sbin/srvr-restic`:

```sh
#!/bin/sh
set -eu
set -a
. /etc/srvr-backup/server.env
set +a
exec restic "$@"
```

```sh
sudo /usr/local/sbin/srvr-restic init
sudo /usr/local/sbin/srvr-restic snapshots
```

**Check:** the repository opens with the server key. Confirm you can also open it from your admin computer using the saved password and maintenance key.

## 3. Schedule the backup

Save this as `/usr/local/sbin/srvr-backup`, root-owned and mode `0700`:

```sh
#!/bin/sh
set -eu
umask 077
exec 9>/run/srvr-backup.lock
flock -n 9
set -a
. /etc/srvr-backup/server.env
set +a
env HOME="$SERVER_HOME" "$SRVR_BIN" snapshot --all \
  --to /var/backups/srvr/services
set -- \
  /var/backups/srvr/services "$SERVER_HOME/services" "$SERVER_HOME/.config" \
  "$SERVER_HOME/.local/state/srvr" "$SERVER_HOME/SERVER.md" \
  /etc
for path in "$SERVER_HOME/.local/lib/srvr" "$SERVER_HOME/.local/bin"; do
  if [ -d "$path" ]; then set -- "$@" "$path"; fi
done
restic backup --tag nightly "$@"
day=$(date -u +%-j)
part=$((day % 30 + 1))
restic check --read-data-subset "$part/30"
```

Each app briefly stops while srvr copies its data, then returns to its previous running state. A snapshot failure stops the upload job. Schedule deployments outside this window: data copies are consistent per service, but code/config and all apps are not one atomic snapshot. The rotating restic check reads the repository in portions, roughly monthly.

Create `/etc/systemd/system/srvr-backup.service`:

```ini
[Unit]
Description=Back up srvr services off-server
Wants=network-online.target
After=network-online.target docker.service tailscaled.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/srvr-backup
TimeoutStartSec=4h
```

Create `/etc/systemd/system/srvr-backup.timer`:

```ini
[Unit]
Description=Nightly srvr backup

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
```

The timer uses the server's timezone (`timedatectl`). Create the offline index and run once before enabling the schedule:

```sh
srvr info --all --markdown > ~/SERVER.md
sudo systemctl daemon-reload
sudo systemctl start srvr-backup.service
sudo journalctl -u srvr-backup.service -n 30
sudo /usr/local/sbin/srvr-restic snapshots --tag nightly
sudo systemctl enable --now srvr-backup.timer
systemctl list-timers srvr-backup.timer
```

**Check:** a new remote snapshot exists, apps are healthy, and the next run is scheduled. Add [backup alerts](monitoring.md#3-alert-on-backup-failure) next; local snapshot history alone does not prove an upload succeeded.

## 4. Test a restore

Choose a snapshot ID from `snapshots`, then restore one service into a separate directory:

```sh
sudo /usr/local/sbin/srvr-restic restore SNAPSHOT-ID --target /root/srvr-restore \
  --include /var/backups/srvr/services/notes
```

The restored data is under `/root/srvr-restore/var/backups/srvr/services/notes/`, followed by its original absolute path. Check files, numeric owners, permissions, and database integrity. Start a disposable app with this copy and matching code/image versions on a different loopback port. Disable external sync and other side effects during the test.

For an actual recovery, give srvr the complete tested service snapshot directory, including its `.srvr-data.json` manifest when it contains named volumes:

```sh
srvr restore notes --snapshot /root/srvr-restore/var/backups/srvr/services/notes
srvr health notes
```

srvr stops the project, stages all directories and volumes, restores numeric ownership/modes, retains previous data and checks readiness. Keep the set-aside data until you have verified the app's contents. For a local archive, use `srvr restore notes latest` instead. Do not manually rename a Docker volume's `_data` root or remove a Docker volume.

**Lost host:** follow [Bootstrap](bootstrap.md), recover backup credentials from the vault, restore app code/config and registry with original ownership, and selectively reapply `/etc` settings. Recreate the same registered plain local volume names/project ownership before restoring their data. Archives/snapshots map those names into the new daemon's validated data root; unknown mappings and same-host replaced volume identities are refused. Authenticate Tailscale again and recreate read-only Git deploy keys. If restored srvr state contains `images.compose.json`, captured image IDs may not exist: pull/build the registered versions, remove only unavailable runtime pins, then start. Match app versions to the restored data; see [image recovery](updates.md#image-pins-and-host-recovery).

**Check:** you can open the restored app and recover the backup password without the original server.

## Local restore recovery

`srvr restore` verifies an archive's checksum or snapshot's identity/content and stages every source beside its destination before switching live data. Directory restores rename directories; named-volume restores journal movement of contents while preserving the `_data` root identity. Old data remains under `*.pre-restore-*`, and failed installed contents may remain under `*.failed-restore-*`. Ownership and modes are preserved. Data directories must be distinct and must not contain one another or overlap Docker storage. Plan space for staged contents and retained data.

If srvr is interrupted during a restore, retry `srvr restore SERVICE BACKUP` or the same `--snapshot DIR` with unchanged registered data paths and volume identities. It first recovers the unfinished switch, then attempts the restore. If recovery fails, the service stays stopped and the journal remains at `~/.local/share/srvr/backups/SERVICE/.restore.json`; start, restart and other mutations, including raw Compose `up`/`run`, are blocked. Keep the journal and set-aside data until recovery succeeds. An unfinished volume restore cannot be moved to another daemon/storage identity. Check journal state after a host reboot before starting containers through Docker directly; Docker restart policies bypass srvr's guard.

Older directory-only archives and snapshots remain usable for directory-only registrations. They cannot supply newly registered volumes: take a complete new backup after changing coverage. Preserve the complete service snapshot directory when uploading/restoring it.

A restore across several directories is recoverable, not one atomic filesystem operation. Snapshot replacement similarly retains its previous directory during publication and recovers an interrupted swap on retry. Off-server upload should run only after a successful snapshot command.

Local retention keeps the newest configured number of archives **plus** the backup referenced by the last deployment, so `rollback --data` remains possible. Archives created in the same second receive distinct names.

## 5. Maintain a second copy

Monthly, from the admin computer with its maintenance credentials:

- Preview retention with `restic forget --dry-run --group-by host,tags --keep-within-daily 7d --keep-within-weekly 1m --keep-within-monthly 1y`; inspect the kept snapshots before repeating without `--dry-run` and adding `--prune`.
- Run `restic check`. B2 hidden versions remain billed until their lifecycle window expires.
- [Copy snapshots](https://restic.readthedocs.io/en/stable/045_working_with_repos.html#copying-snapshots-between-repositories) to an independently encrypted restic repository on an external disk, check it, and disconnect it.
- Refresh the offline index and encrypted recovery vault. Include LUKS header backups if you use disk encryption; store them as secrets outside the encrypted host.

Keep deletion credentials off the server. Repeat the restore test after changing backup coverage or storage, and periodically thereafter. [Restic's retention guide](https://restic.readthedocs.io/en/stable/060_forget.html) explains the options.
