# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate operational files from the templates in deploy/ (DESIGN.md
section 13). Standard library only.

install.sh calls `python3 -m emptydiff deploy-render` and copies the
result to the target locations. All values come from
/etc/emptydiff/config.toml and the installation directory; nothing is
hard-wired in the repo.
"""

import ipaddress
import os
import re
from urllib.parse import urlsplit

from . import auth, config

TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "deploy"
)

# template -> target file relative to the output directory
OUTPUTS = {
    "apache.conf.in": "apache/emptydiff.conf",
    "emptydiff-worker.service.in": "systemd/emptydiff-worker.service",
    "emptydiff-purge.service.in": "systemd/emptydiff-purge.service",
    "emptydiff-purge.timer.in": "systemd/emptydiff-purge.timer",
    "logrotate.in": "logrotate/emptydiff",
}
# its file name is that of the mount unit, which follows from the data
# directory
MOUNT_TEMPLATE = "emptydiff-run.mount.in"
# Apache has one variant per URL scheme; the directives both share live
# in one file that is inserted at @VHOST_BODY@
APACHE_TEMPLATE = {"https": "apache.conf.in", "http": "apache-http.conf.in"}
VHOST_BODY_TEMPLATE = "apache-vhost.in"

HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
# plain HTTP: also single-label names ("labserver") and IPv4 addresses
HTTP_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)
PATH_RE = re.compile(r"^/[A-Za-z0-9/._-]+$")
PLACEHOLDER_DOMAINS = (
    "example.org",
    "example.com",
    "example.net",
    "example.edu",
)
BODY_SLACK = 1_000_000  # multipart overhead above max_upload_bytes


class DeployError(Exception):
    pass


def mount_unit_name(path):
    """The name systemd requires for the mount unit of `path` (what
    `systemd-escape -p --suffix=mount` prints)."""
    parts = [p for p in path.split("/") if p]
    escaped = []
    for part in parts:
        escaped.append(
            "".join(
                (
                    c
                    if c.isalnum() and c.isascii() or c in ":_."
                    else f"\\x{ord(c):02x}"
                )
                for c in part
            )
        )
    name = "-".join(escaped)
    if name.startswith(
        "."
    ):  # systemd escapes a dot only at the very start of the name
        name = "\\x2e" + name[1:]
    return name + ".mount"


def _is_ipv4(host):
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_placeholder(domain):
    return any(
        domain == d or domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS
    )


def values(cfg, app_dir, accept_acme_terms=False):
    """Substitution values for the templates; checks the configuration
    for required settings and placeholders."""
    try:
        config.check_production(cfg)
    except config.ConfigError as e:
        raise DeployError(str(e)) from None
    data_dir = str(cfg["data_dir"]).rstrip("/")
    if not PATH_RE.match(data_dir):
        raise DeployError(
            f"Invalid data_dir: {cfg['data_dir']!r} (only "
            "letters, digits and / . _ -)"
        )
    if not PATH_RE.match(app_dir or ""):
        raise DeployError(
            f"Invalid installation directory: {app_dir!r} (only "
            "letters, digits and / . _ -)"
        )

    url = urlsplit(cfg["base_url"])
    host = (url.hostname or "").lower()
    scheme = url.scheme
    if scheme == "https":
        host_ok = (
            bool(HOST_RE.match(host)) and not host.rpartition(".")[2].isdigit()
        )  # IP addresses do not get an ACME certificate
        form = "https://<hostname>"
    elif scheme == "http":
        host_ok = bool(HTTP_HOST_RE.match(host)) and (
            not host.rpartition(".")[2].isdigit() or _is_ipv4(host)
        )
        form = "http://<hostname or IPv4 address> (closed networks only)"
    else:
        host_ok, form = False, "https://<hostname>"
    if (
        not host_ok
        or url.path not in ("", "/")
        or url.port
        or url.query
        or url.fragment
    ):
        raise DeployError(
            f"base_url must have the form {form} "
            f"(without path and port), not {cfg['base_url']!r}"
        )
    if scheme == "https" and not accept_acme_terms:
        raise DeployError(
            "The terms of service of the certificate authority "
            "(ACME, e.g. Let's Encrypt) must be "
            "accepted explicitly: install.sh --accept-acme-terms"
        )
    admin = cfg["auth"]["admins"][0].strip().lower()
    if not auth.EMAIL_RE.match(admin):
        raise DeployError(f"auth.admins: invalid address {admin!r}")
    log_days = cfg["privacy"]["log_days"]
    if (
        not isinstance(log_days, int)
        or isinstance(log_days, bool)
        or not 1 <= log_days <= 365
    ):
        raise DeployError("privacy.log_days: integer from 1 to 365 expected")

    placeholders = [
        d
        for d in (
            host,
            str(cfg["mail"]["host"]).lower(),
            cfg["mail"]["sender"].rpartition("@")[2].lower(),
            admin.rpartition("@")[2],
            *[d.lower() for d in cfg["auth"]["allowed_domains"]],
        )
        if _is_placeholder(d)
    ]
    if placeholders:
        raise DeployError(
            "The configuration still contains placeholders ("
            + ", ".join(sorted(set(placeholders)))
            + "). Please fill in /etc/emptydiff/config.toml."
        )
    return {
        "SCHEME": scheme,
        "HOST": host,
        "ADMIN_EMAIL": admin,
        "APP_DIR": app_dir.rstrip("/"),
        "DATA_DIR": data_dir,
        "MAX_BODY": str(cfg["limits"]["max_upload_bytes"] + BODY_SLACK),
        "MAX_BACKUP_BODY": str(
            cfg["limits"]["max_backup_bytes"] + 2 * BODY_SLACK
        ),
        "LOG_DAYS": str(log_days),
        "MD_AGREEMENT": "accepted",
    }


def _read(template_dir, name):
    with open(os.path.join(template_dir, name), encoding="utf-8") as f:
        return f.read()


def _strip_comments(text):
    """The text without its leading comment block (license header and
    note about the file) and without the final line break."""
    lines = text.split("\n")
    while lines and (not lines[0].strip() or lines[0].startswith("#")):
        lines.pop(0)
    return "\n".join(lines).rstrip("\n")


def render(cfg, app_dir, out_dir, accept_acme_terms=False, template_dir=None):
    """Write all operational files to `out_dir`; returns the relative
    paths."""
    vals = values(cfg, app_dir, accept_acme_terms)
    template_dir = template_dir or TEMPLATE_DIR
    written = []
    outputs = {
        **OUTPUTS,
        MOUNT_TEMPLATE: "systemd/"
        + mount_unit_name(vals["DATA_DIR"] + "/run"),
    }
    apache = APACHE_TEMPLATE[vals["SCHEME"]]
    outputs[apache] = outputs.pop("apache.conf.in")
    for template, target in outputs.items():
        text = _read(template_dir, template)
        if template == apache:
            body = _read(template_dir, VHOST_BODY_TEMPLATE)
            text = text.replace("@VHOST_BODY@", _strip_comments(body))
        text = re.sub(
            r"@([A-Z_]+)@",
            lambda m: vals[m.group(1)] if m.group(1) in vals else m.group(0),
            text,
        )
        leftover = re.search(r"@[A-Z_]+@", text)
        if leftover:
            raise DeployError(
                f"{template}: unbekannter Platzhalter {leftover.group()}"
            )
        path = os.path.join(out_dir, target)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written.append(target)
    return written
