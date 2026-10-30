# EmptyDiff

EmptyDiff is a small, self-hosted web application for programming lab courses. Students log in with a one-time code sent to their institutional email address, upload their program files, and get an automatic check within seconds. Currently Octave/MATLAB (`.m` files) is supported; the design leaves room for other languages. Lecturers set up the labs, follow the submissions and export the results.

It replaces an older mail-based submission system from 2015 and is built to be simple to run and easy to understand: few dependencies, one configuration file, one SQLite database.

> Status: alpha. Not yet used in production.

## Features

- **Students:** login by one-time code (no passwords), upload per lab, report with a tick or cross for every check, up to a configurable number of attempts per lab. Late submissions are accepted and marked. German and English interface.
- **Lecturers:** manage courses and labs in the browser, view and download submissions, grade them again after fixing a testbench, credit list per course, backup and restore, cloning a course for the next semester.
- **Checks:** a testbench is a few lines of code (Octave today; the system is built for more languages) using `check(...)` and `check_close(...)`. It runs in a sandbox (no network, time and memory limits) and the verdict never depends on what the student's program prints.
- **Privacy:** submissions are deleted automatically after a retention period (default 2 years), pseudonymized file paths, no tracking or external resources.

A testbench looks like this:

```octave
x = addone(1);
check(x == 2, 'addone(1) returns 2', 'addone(1) does not return 2');
check_close(addone([1 2]), [2 3], 1e-12, 'works for vectors', 'wrong for vectors');
```

## Requirements

A Debian 13 server with root access, ports 80 and 443 reachable from the internet (or a reverse proxy that serves it under a path, see INSTALL.md), a DNS name, an SMTP server for the login mails, and unprivileged user namespaces in the kernel (Debian default).

Everything comes from Debian packages, there is no `pip`, no Node and no CDN:

- Apache with `mod_wsgi` (certificates through `mod_md`)
- Python 3 (standard library) and `python3-flask`
- `bubblewrap` and `util-linux` (sandbox and resource limits)
- Octave, with `gnuplot-nox` and `fonts-freefont-otf` so that plots work without a display
- systemd and logrotate

## Install

```bash
sudo apt-get install -y git
sudo git clone https://github.com/andreas-schwenk/emptydiff /opt/emptydiff
cd /opt/emptydiff
sudo ./install.sh --accept-acme-terms   # first run creates /etc/emptydiff/config.toml and stops
sudo nano /etc/emptydiff/config.toml      # server name, allowed domains, admins, mail, privacy details
sudo ./install.sh --accept-acme-terms   # installs packages, Apache, services; ends with a health check
```

See [INSTALL.md](INSTALL.md) for updates, operation, backups and troubleshooting.

## Security

What the system does for it (details in [DESIGN.md](DESIGN.md), section 14):

- **Login:** one-time codes by mail instead of passwords (8 characters, 15 minutes, single use, stored only as a hash, five tries per code). Only addresses of the allowed domains get a mail. Requests for codes are rate-limited per address and per IP address, with a cooldown, a hidden form field and a minimum form time against bots. No captcha and no third-party service.
- **Web application:** CSRF token on every form, signed session cookie (`HttpOnly`, `Secure` over https, `SameSite`), strict Content-Security-Policy (no inline scripts or styles, no external resources), `nosniff`, HSTS (https), no indexing by search engines, escaped output. Students see only their own submissions; the administration is for the configured admins only.
- **Uploads:** only files with the names of the required files, size limits, validated file names, storage under pseudonymized paths. Images are served only if the content really is an image (never SVG).
- **Running student code:** every submission runs in a `bubblewrap` sandbox: no network, no access to the host or the database, read-only system directories, no capabilities, own process namespace, cleared environment. On top come limits for CPU time, memory, file size, processes, run time and output; a size-limited `/tmp`; run folders on a RAM disk of fixed size; memory, task and network limits for the whole worker service. One submission at a time, and the worker refuses to start if the sandbox self-test fails.
- **Verdict:** it comes from a results file written by `check` and `check_close`, not from printed text. These functions cannot be called by student code, the end of the run is recorded by the system, and submissions that contain suspicious words (`exit`, `evalin`, `fopen`, ...) are flagged for admins.
- **Data:** hand-written SQL with parameters only, automatic deletion after the retention period, backups that are fully validated (checksums, paths, sizes) before a restore, access logs deleted after a few days. Secrets live only in `/etc/emptydiff/config.toml`, never in the repository.
- **Operation:** a dedicated service user, systemd hardening (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`), only Debian packages, a health check for monitoring.

Known limits: student code and the testbench run in the same Octave process, so a deliberate attack on the check is hindered and flagged but not impossible; there is no syscall filter (seccomp) yet; the sandbox needs unprivileged user namespaces, so keep the kernel and `bubblewrap` up to date. The full `install.sh` run with a certificate on a real server is still to be verified (it ran on a Raspberry Pi in the plain-HTTP mode for closed networks, see INSTALL.md).

## Development

Docker is used only for development (Debian 13 with Flask, bubblewrap and Octave), never in production.

```bash
dev/docker.sh up       # container plus web server on http://127.0.0.1:8091 (login mails appear as a dialog)
dev/docker.sh test     # unit tests
dev/docker.sh sandbox  # checks the sandbox
```

Tests also run natively with `python3 -m unittest discover -s tests` (those needing Flask or the sandbox are skipped).

## Documentation

- [INSTALL.md](INSTALL.md): installation and operation (English)
- [DESIGN.md](DESIGN.md): design decisions and details (German)
- [config.example.toml](config.example.toml): all settings with their defaults

## License

GNU General Public License v3.0 or later, see [LICENSE](LICENSE). Parts of the code were written with the help of AI (Claude, Anthropic). By Andreas Schwenk, [arts-and-sciences.com](https://arts-and-sciences.com).
