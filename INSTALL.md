# Installation and operation

EmptyDiff runs on a Debian server (tested on Debian 13) with Apache (`mod_wsgi`, certificates via `mod_md`), a worker service for grading and a daily deletion run. Only Debian packages are used (no `pip`, no Docker, no Node).

## Requirements

- A Debian server with root access, reachable from the internet on ports **80 and 443** (for the certificate).
- A DNS name that points to the server.
- An SMTP server through which the server may send mail to the students (login codes).
- Unprivileged user namespaces in the kernel (Debian default; `bubblewrap` needs them).

## First installation

```bash
sudo apt-get install -y git
sudo git clone https://github.com/andreas-schwenk/emptydiff /opt/emptydiff
cd /opt/emptydiff
sudo ./install.sh --accept-acme-terms
```

On the first run the script creates `/etc/emptydiff/config.toml` as a template and stops. Fill in this **one** configuration file (server name, allowed domains, admins, mail server, privacy details), then run it again:

```bash
sudo nano /etc/emptydiff/config.toml
sudo ./install.sh --accept-acme-terms
```

`--accept-acme-terms` confirms the terms of service of the certificate authority (e.g. Let's Encrypt). Without this option the script refuses to install. The server then obtains the certificate by itself and renews it automatically.

The script installs the packages, creates the user `emptydiff` and the directories, generates the files for Apache, systemd and logrotate from `deploy/` (including a RAM disk of fixed size for the run folders of the checks, mounted below the data directory), creates the database, checks the sandbox (`selftest`) and starts the services. It ends with a health check.

Check mail (sends a real mail to the given address):

```bash
cd /opt/emptydiff && sudo -u emptydiff PYTHONPATH=/opt/emptydiff python3 -m emptydiff mail-test address@example.org
```

Create the labs in the web interface (Administration), or restore a “labs only” archive under Administration → Backup.

## Plain HTTP for closed networks (tests, intranet)

If the server has no public DNS name (a test server in a home or lab network, for example a Raspberry Pi), set `base_url` to `http://` plus the host name or IPv4 address, without path and port:

```toml
base_url = "http://192.0.2.10"
```

`install.sh` then installs Apache with a single site on port 80: no `mod_md`, no certificate, no redirect, no HSTS, and `--accept-acme-terms` is not needed. The cookies are not marked `Secure` (a browser would refuse to send them over http). The other settings stay as above; the mail server and the privacy details must still be filled in, and the placeholder domains (`example.org`, `example.edu` …) are still refused, so use names of your own, for example `pi.test`.

**Login codes, session cookies and submissions travel unencrypted in this mode. Use it only in a closed, trusted network, never on the internet.** The installation prints a warning, disables Apache's default site (otherwise it would answer requests by IP address) and keeps everything else. Switching to https later means changing `base_url` and running `install.sh --accept-acme-terms` again.

Without a mail server, request a code on the login page (it reports that the mail could not be sent but still opens the page for the code), then print the code with `python3 -m emptydiff issue-code <address>` (see Operation).

## Update

```bash
cd /opt/emptydiff
sudo git pull            # or: sudo git fetch && sudo git checkout <tag>
sudo ./install.sh --accept-acme-terms
```

The script rewrites all operational files from the templates and restarts the services. **Untouched** are `/etc/emptydiff/config.toml` and `/var/lib/emptydiff` (database with labs, testbenches and submissions). The database is migrated to the new schema automatically on start; migrations only go forward.

**The system makes no backups by itself.** Before an update an admin downloads a full backup (Administration → Backup, see below). Backed-up data are subject to the same retention period (`retention_days`) as the originals and must be deleted as soon as it expires.

Your own changes to the generated files (`/etc/apache2/sites-available/emptydiff.conf`, `/etc/systemd/system/emptydiff-*`, `/etc/logrotate.d/emptydiff`) are lost on the next update. Settings belong in the configuration file; to customize the templates, change `deploy/`.

## Operation

| What | How |
|---|---|
| Health | `GET /healthz` (200 `{"status": "ok"}` or 503 with problem identifiers `db`, `worker`, `queue`, `purge`, without personal data) or `python3 -m emptydiff healthcheck` (exit 1 on problems); suitable for external monitoring |
| Worker | `systemctl status emptydiff-worker`, log: `journalctl -u emptydiff-worker` |
| Deletion | daily at 03:30 (`emptydiff-purge.timer`); log `journalctl -u emptydiff-purge`; manually `python3 -m emptydiff purge [--dry-run]` |
| Web server | `/var/log/apache2/emptydiff/{access,error}.log` (contain IP addresses; deleted by logrotate after `privacy.log_days` days) |
| Emergency login | `python3 -m emptydiff issue-code <address>` if mail delivery fails: the person asks for a code on the login page (an error about the mail is shown, the page for the code opens anyway), the supervisor reads out the code from the command |
| State | One SQLite file and a directory with submissions under `/var/lib/emptydiff` |

Run the `python3 -m emptydiff …` commands on the server as the user `emptydiff` in the installation directory:
`cd /opt/emptydiff && sudo -u emptydiff PYTHONPATH=/opt/emptydiff python3 -m emptydiff <command>`.

## Programming languages

Every lab has a programming language. A server offers the languages listed in `languages.enabled` of `/etc/emptydiff/config.toml` (default: `["octave"]`); `install.sh` installs the Debian packages of exactly these languages (`python3 -m emptydiff packages` lists them) and the self-test checks every interpreter. A section `[languages.<id>]` can change the name of the interpreter (`command`) and the words that mark a submission as conspicuous (`suspicious_words`). A lab in a language that the server does not offer takes no submissions; its language cannot be changed once there are submissions. To add a language to a running server, add it to `languages.enabled` and run `install.sh` again.

## Limits for student programs

Student programs run in a sandbox with limits per process. In addition the whole worker service is limited (`MemoryMax=2G`, `TasksMax=200`, no network) and the run folders live on a RAM disk of 256 MB (`/var/lib/emptydiff/run`, a systemd mount unit), so a program that writes or allocates without end cannot fill the data disk or starve the web server. The values are in `deploy/emptydiff-worker.service.in` and `deploy/emptydiff-run.mount.in`; adjust them to the size of the server and run `install.sh` again. `systemctl status emptydiff-worker` and `systemctl show emptydiff-worker -p MemoryCurrent` show the use.

## Testing on a Raspberry Pi

Tested on one Raspberry Pi 4 Model B (8 GB, SD card, Raspberry Pi OS Lite 64-bit, Debian 13) in plain-HTTP mode: `install.sh`, the sandbox self-test, the worker service with its memory limit and all unit tests ran. A burst of 200 students uploading at once (`dev/loadtest.py`, 40 uploads at a time) was checked completely in about 56 s (roughly 210 checks per minute for a small function; upload time median 0.4 s, at most 2 s), all passed, the CPU stayed below 60 °C without throttling. Not tried yet: TLS with `mod_md`, real mail, labs with plots, programs that exhaust memory. A Raspberry Pi 4 or 5 can run EmptyDiff for small instances:

- Use **Raspberry Pi OS Lite (64-bit)** (based on Debian 13). One SD card with a current image boots on a Pi 4 and a Pi 5. The 32-bit image does not report `ID=debian`, and Octave and Flask are packaged for `arm64`.
- Headless setup: in the Raspberry Pi Imager set the host name, user, **SSH with your public key**, network and locale before writing the card. Then `ssh user@host.local`, `sudo apt update && sudo apt full-upgrade`, and follow "First installation".
- **Memory limits:** the Raspberry Pi kernel may ship with the memory cgroup disabled, which makes `MemoryMax` of the worker service useless. Check `cat /sys/fs/cgroup/cgroup.controllers` for `memory`; if it is missing, add `cgroup_enable=memory cgroup_memory=1` to the line in `/boot/firmware/cmdline.txt` and reboot.
- **RAM:** the worker may use up to 2 GiB plus the 256 MB RAM disk, so use 4 GB or more. Octave starts slower than on a server; raise `grader.timeout` if needed. An SD card is enough for tests; a USB SSD lasts longer.
- **Mail and certificate:** without a public name there is no `mod_md` certificate and probably no SMTP server. Use plain HTTP (see "Plain HTTP for closed networks") and create login codes with `python3 -m emptydiff issue-code <address>`.

## Backup

Admins back up and restore in the web interface under **Administration → Backup**:

- **Full backup (ZIP):** all labs and submissions. Not included: logins, secret key, configuration (the operator backs up `/etc/emptydiff/config.toml` separately). The archive is not encrypted and contains personal data.
- **Labs only (ZIP):** without any student data.
- **Restoring:** upload the archive; it is checked completely first. A full backup **replaces the entire data set** (confirmed by typing the word `REPLACE`, `ERSETZEN` in the German interface); during the restore submissions are briefly blocked. A labs archive adds labs (inactive) or overwrites labs of the same name.
- **Size:** at most `limits.max_backup_bytes` (default 500 MB). The file and the unpacked copy briefly live in the data directory (`/var/lib/emptydiff/tmp`, `import-staging`); there must be enough space.
- **New server:** install the server as described above (create the configuration), then restore the full backup.

## Troubleshooting

- **Installation stops at the self-test:** `sh dev/check-sandbox.sh` shows which part of the sandbox (bubblewrap, Octave, prlimit) fails. Usually user namespaces or packages are missing.
- **A submission shows an error instead of a result, `error_log` says `bwrap: …`:** the sandbox did not start (for example `Creating new namespace failed: Resource temporarily unavailable` when the user `emptydiff` has too many processes; the process limit of a check is `prlimit --nproc=128` for all processes of that user). The worker tries three times before it stores an error; such a submission is never marked as failed. Regrade it with "Regrade" in the submission list; if it keeps happening, look at what else runs as `emptydiff` (the Apache daemon processes) and at `/var/log/apache2/emptydiff/error.log`.
- **`/healthz` reports `worker`:** the service is not running or the database is not writable (`journalctl -u emptydiff-worker`).
- **`/healthz` reports `queue`:** submissions have been waiting for more than 10 minutes (worker stuck or overloaded; submissions are graded strictly one after another; check the time limit and `grader.batch_size`).
- **`/healthz` reports `purge`:** the deletion run is overdue (`systemctl list-timers`, `journalctl -u emptydiff-purge`).
- **No certificate:** are ports 80/443 reachable from the internet? Does the DNS name match `base_url`? Errors are in `/var/log/apache2/emptydiff/error.log` and `apache2ctl -t -D DUMP_MODULES | grep md`.
- **Restore fails:** the message gives the reason (damaged or modified archive, too large, newer EmptyDiff version). With "A submission is still being evaluated" try again later. If space is short, check `/var/lib/emptydiff`. The old data set is kept on every error.
- **No login mail:** run `mail-test`; check the SMTP settings in the `[mail]` section.
