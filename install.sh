#!/usr/bin/env bash
# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

# EmptyDiff: installation and update on Debian (idempotent).
#
#   First install:  git clone <repo> /opt/emptydiff && cd /opt/emptydiff && sudo ./install.sh --accept-acme-terms
#   Update:         cd /opt/emptydiff && git pull && sudo ./install.sh --accept-acme-terms
#   (With base_url = "http://..." in the configuration - closed networks only - the option is not needed.)
#
# The script rewrites the operational files (Apache, systemd, logrotate) from the templates in deploy/ every time.
# It never touches: /etc/emptydiff/config.toml, /var/lib/emptydiff (database with labs, submissions).
# The database is migrated to the new schema automatically on start. The system makes no backups
# by itself; back up /var/lib/emptydiff yourself before an update if needed.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG_DIR=/etc/emptydiff
CONFIG=$CONFIG_DIR/config.toml
DATA_DIR=/var/lib/emptydiff
LOG_DIR=/var/log/apache2/emptydiff
SERVICE_USER=emptydiff
PACKAGES=(apache2 libapache2-mod-wsgi-py3 python3 python3-flask bubblewrap util-linux)  # the packages of the programming languages follow
ACME_FLAG=()

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
as_service_user() { (cd "$APP_DIR" && runuser -u "$SERVICE_USER" -- env PYTHONPATH="$APP_DIR" python3 -m emptydiff "$@"); }

for arg in "$@"; do
  case "$arg" in
    --accept-acme-terms) ACME_FLAG=(--accept-acme-terms) ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Please run as root (sudo ./install.sh --accept-acme-terms)."
command -v apt-get >/dev/null || die "apt-get not found: only Debian (and derivatives) is supported."
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = debian ] || echo "Warning: tested on Debian 13, this system reports '${ID:-?}'." >&2

say "Installing packages (Debian packages only, no pip)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends "${PACKAGES[@]}"
# Packages of the programming languages that the server offers (languages.enabled in the configuration; the defaults
# before the configuration exists). Only the standard library of Python is needed to ask for them.
if [ -e "$CONFIG" ]; then LANGUAGE_ARGS=(--config "$CONFIG" packages); else LANGUAGE_ARGS=(packages --defaults); fi
LANGUAGE_PACKAGES="$(cd "$APP_DIR" && PYTHONPATH="$APP_DIR" python3 -m emptydiff "${LANGUAGE_ARGS[@]}")"  # stops the script on an error
# shellcheck disable=SC2086  # one package name per line, no spaces inside
[ -z "$LANGUAGE_PACKAGES" ] || apt-get install -y --no-install-recommends $LANGUAGE_PACKAGES

if [ "$(sysctl -n kernel.unprivileged_userns_clone 2>/dev/null || echo 1)" = 0 ]; then
  die "kernel.unprivileged_userns_clone = 0: bubblewrap needs unprivileged user namespaces (sysctl kernel.unprivileged_userns_clone=1)."
fi

say "User and directories"
getent passwd "$SERVICE_USER" >/dev/null || \
  adduser --system --group --home "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"
install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"
install -d -o root -g adm -m 0750 "$LOG_DIR"
chmod -R a+rX "$APP_DIR/emptydiff" "$APP_DIR/deploy"   # Apache serves /static directly; the service user reads the code

if [ ! -e "$CONFIG" ]; then
  install -o root -g "$SERVICE_USER" -m 0640 "$APP_DIR/deploy/config.production.toml" "$CONFIG"
  echo
  echo "The configuration $CONFIG was created as a template."
  echo "Please fill it in (server name, allowed domains, admins, mail server, privacy details) and run install.sh again."
  exit 3
fi
chown root:"$SERVICE_USER" "$CONFIG"
chmod 0640 "$CONFIG"

say "Generating operational files from the templates"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
(cd "$APP_DIR" && PYTHONPATH="$APP_DIR" python3 -m emptydiff deploy-render --app-dir "$APP_DIR" --out "$STAGE" "${ACME_FLAG[@]}") >/dev/null \
  || die "The operational files could not be generated (see message above)."

say "Creating the database or migrating it to the new schema"
as_service_user initdb

say "Sandbox self-test (bubblewrap and the interpreters of the offered languages)"
as_service_user selftest || die "The sandbox self-test failed. The worker is not started; see dev/check-sandbox.sh."

say "Installing files"
# RAM disk of fixed size for the run folders of the checks (the file name of the unit follows from the path)
MOUNT_FILE="$(ls "$STAGE"/systemd/*.mount)"
MOUNT_UNIT="$(basename "$MOUNT_FILE")"
RUN_DIR="$(sed -n 's/^Where=//p' "$MOUNT_FILE")"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$RUN_DIR"
install -m 0644 "$MOUNT_FILE" "/etc/systemd/system/$MOUNT_UNIT"
install -m 0644 "$STAGE/systemd/emptydiff-worker.service" /etc/systemd/system/emptydiff-worker.service
install -m 0644 "$STAGE/systemd/emptydiff-purge.service" /etc/systemd/system/emptydiff-purge.service
install -m 0644 "$STAGE/systemd/emptydiff-purge.timer" /etc/systemd/system/emptydiff-purge.timer
install -m 0644 "$STAGE/logrotate/emptydiff" /etc/logrotate.d/emptydiff
install -m 0644 "$STAGE/apache/emptydiff.conf" /etc/apache2/sites-available/emptydiff.conf

say "Setting up Apache"
# https (mod_md certificate) or, for closed networks, plain http: the generated site tells which
if grep -q 'SSLEngine on' "$STAGE/apache/emptydiff.conf"; then
  PLAIN_HTTP=0
  a2enmod ssl md headers wsgi >/dev/null
else
  PLAIN_HTTP=1
  a2enmod headers wsgi >/dev/null
  # Apache's own default site would answer requests by IP address or an unknown name
  a2dissite 000-default >/dev/null 2>&1 || true
fi
a2ensite emptydiff >/dev/null
apache2ctl configtest

say "Starting (restarting) services"
systemctl daemon-reload
systemctl stop emptydiff-worker.service 2>/dev/null || true   # the RAM disk cannot be remounted while it is in use
systemctl enable "$MOUNT_UNIT" >/dev/null
systemctl restart "$MOUNT_UNIT"
systemctl enable emptydiff-worker.service emptydiff-purge.timer >/dev/null
systemctl start emptydiff-worker.service
systemctl restart emptydiff-purge.timer
systemctl enable apache2 >/dev/null
systemctl restart apache2

say "Check"
ok=0
for _ in $(seq 1 20); do
  if as_service_user healthcheck >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -eq 1 ]; then
  echo "EmptyDiff is running. The state can be checked at any time with: python3 -m emptydiff healthcheck or GET /healthz"
else
  echo "Warning: the health check reports problems:" >&2
  as_service_user healthcheck >&2 || true
  echo "Logs: journalctl -u emptydiff-worker -u emptydiff-purge; /var/log/apache2/emptydiff/error.log" >&2
  exit 2
fi
if [ "$PLAIN_HTTP" -eq 1 ]; then
  echo "Warning: plain http (base_url starts with http://): login codes and cookies are not encrypted. Use this only in a closed, trusted network." >&2
else
  echo "The server must be reachable on ports 80 and 443 so that mod_md can issue the certificate."
fi
