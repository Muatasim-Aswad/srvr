# Monitor health and backups

Use an external heartbeat so a powered-off or disconnected server can still trigger an alert. This recipe uses Healthchecks.io; the server only sends outbound HTTPS requests.

[Bootstrap](bootstrap.md) · [Backups](backups.md) · **Monitoring**

**Route:** [Checks](#1-create-two-checks) → [Heartbeat](#2-send-server-health) → [Backup alerts](#3-alert-on-backup-failure) → [Disk tests](#4-schedule-disk-self-tests) → [Verify](#5-test-and-operate).

Use the backup job from [Backups](backups.md) first. Replace `/home/admin` with your admin home. The examples assume default srvr paths. Create scripts with `sudoedit`, then set mode `0700` with `sudo chmod 0700 <path>`; keep them root-owned.

## 1. Create two checks

Create a Healthchecks.io project and connect an alert destination you will notice, such as email or ntfy.

| Check | Schedule | Grace |
|---|---|---|
| Server health | Every 5 minutes | 10 minutes |
| Off-server backup | Cron `0 4 * * *`, server's timezone | 4 hours |

Use `timedatectl` to match the backup check to the timer. The grace covers its randomized delay and runtime. Healthchecks supports [start/completion pings](https://healthchecks.io/docs/measuring_script_run_time/) and [explicit failure pings](https://healthchecks.io/docs/signaling_failures/).

```sh
sudo install -d -m 0700 /etc/srvr-monitor
sudo install -m 0600 /dev/null /etc/srvr-monitor/healthchecks.env
```

Use `sudoedit` to fill this file with each check's unique ping URL:

```sh
HC_HEALTH_URL=https://hc-ping.com/YOUR-HEALTH-CHECK-ID
HC_BACKUP_URL=https://hc-ping.com/YOUR-BACKUP-CHECK-ID
SERVER_HOME=/home/admin
SRVR_BIN=/usr/bin/srvr
```

Set `SRVR_BIN` to the full path printed by `command -v srvr` as your admin user; use `/home/admin/.local/bin/srvr` for a source install.

Treat these URLs as secrets: someone with a URL can forge successful pings. If using a public ntfy topic, use a long random name and keep it private too.

## 2. Send server health

Save this as `/usr/local/sbin/srvr-heartbeat`, root-owned and mode `0700`:

```sh
#!/bin/sh
set -eu
umask 077
. /etc/srvr-monitor/healthchecks.env
log=$(mktemp)
trap 'rm -f "$log"' EXIT
result=0
env HOME="$SERVER_HOME" "$SRVR_BIN" doctor >"$log" 2>&1 || result=$?
curl -fsS --max-time 15 --retry 2 --output /dev/null \
  --data-binary @"$log" "$HC_HEALTH_URL/$result" 2>/dev/null || true
```

The script sends doctor's output and exit status. It finishes successfully even when doctor detects a fault, preventing the heartbeat's own failed unit from poisoning the next health check. If sending fails, missing pings still alert externally.

Create `/etc/systemd/system/srvr-heartbeat.service`:

```ini
[Unit]
Description=Report srvr health
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/srvr-heartbeat
TimeoutStartSec=4min
```

Create `/etc/systemd/system/srvr-heartbeat.timer`:

```ini
[Unit]
Description=Check srvr health every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl start srvr-heartbeat.service
sudo systemctl enable --now srvr-heartbeat.timer
```

**Check:** the Healthchecks event log shows doctor output and a recent successful ping. Warnings alone return success; review them during routine maintenance. Doctor fails on unhealthy apps, failed units, disk faults, and a reboot overdue by the configured threshold.

## 3. Alert on backup failure

Save this as `/usr/local/sbin/srvr-backup-ping`, root-owned and mode `0700`:

```sh
#!/bin/sh
set -eu
. /etc/srvr-monitor/healthchecks.env
case "$1" in
  start) suffix=/start ;;
  finish)
    case "${SERVICE_RESULT:-unknown}" in
      success) suffix= ;;
      *) suffix=/fail ;;
    esac ;;
  *) exit 2 ;;
esac
curl -fsS --max-time 15 --retry 2 --output /dev/null \
  --data-raw "backup: ${SERVICE_RESULT:-starting}" \
  "$HC_BACKUP_URL$suffix" 2>/dev/null || true
```

Create `/etc/systemd/system/srvr-backup.service.d/healthchecks.conf` (make the directory first):

```ini
[Service]
ExecStartPre=/usr/local/sbin/srvr-backup-ping start
ExecStopPost=/usr/local/sbin/srvr-backup-ping finish
```

```sh
sudo systemctl daemon-reload
sudo systemctl start srvr-backup.service
```

**Check:** the external backup check receives start and success events only after snapshot, upload, and repository check finish. A failed run sends `/fail`; a missing completion eventually alerts too. Inspect failures with `sudo journalctl -u srvr-backup.service`.

## 4. Schedule disk self-tests

Doctor reads SMART health and self-test history; `smartd` schedules the tests. Identify supported drives with `sudo smartctl --scan`, then review `/etc/smartd.conf` using the [Debian smartd manual](https://manpages.debian.org/trixie/smartmontools/smartd.conf.5.en.html). For drives supporting scheduled self-tests, a starting point is:

```text
DEVICESCAN -a -s (S/../.././02|L/../../6/03)
```

Replace the existing active scan rule rather than adding a duplicate. This requests short tests daily at 02:00 and long tests Saturdays at 03:00. Long tests can overlap the backup; choose a quieter time if needed. Some drives or USB bridges need device-specific settings.

```sh
sudo systemctl enable --now smartmontools.service
sudo systemctl restart smartmontools.service
sudo journalctl -u smartmontools.service -n 20
```

**Check:** after the first scheduled test, doctor reports recent self-test results. Pending/unreadable sectors or failed tests trigger failure; reallocated sectors and stale tests produce warnings.

## 5. Test and operate

Send a deliberate health failure without breaking an app:

```sh
sudo sh -c '. /etc/srvr-monitor/healthchecks.env; curl -fsS --max-time 15 --output /dev/null --data-raw "manual alert test" "$HC_HEALTH_URL/fail"'
sudo systemctl start srvr-heartbeat.service
```

Repeat for `HC_BACKUP_URL`, then run a successful backup. Confirm down **and** recovery notifications for both checks. For a missing-ping test, temporarily stop the heartbeat timer, wait through its interval and grace, then enable it again.

Routine checks:

```sh
systemctl list-timers srvr-heartbeat.timer srvr-backup.timer
sudo env HOME=/home/admin "$(command -v srvr)" doctor
sudo journalctl -u srvr-backup.service -n 30
```

Pause the relevant external checks before planned downtime; stopping a timer alone causes an alert. Resume checks after maintenance and confirm a new ping. Healthchecks alerts on state changes, so inspect event details when a check stays down.

Thresholds live in `~/.config/srvr/srvr.toml`; defaults include `reboot_fail_days = 3`, `selftest_max_age_days = 8`, `backup_max_age_days = 7`, and `disk_warn_percent = 85`. Backup-age warnings track local srvr backups/snapshots; the separate external backup check tracks the upload job. Keep manual reboot and physical disk-unlock needs in your recovery notes.
