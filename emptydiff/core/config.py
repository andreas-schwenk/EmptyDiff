# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Configuration: TOML file with default values. Standard library
only."""

import copy
import os
import tomllib

from . import i18n, languages

DEFAULT_PATH = "/etc/emptydiff/config.toml"
DEV_FALLBACK_PATH = "config.example.toml"

DEFAULTS = {
    "data_dir": "/var/lib/emptydiff",
    "base_url": "https://localhost",
    "retention_days": 730,
    "ui": {
        # language used when the browser asks for none that is available
        # (language code in emptydiff/strings/)
        "default_language": "EN",
    },
    "auth": {
        # domains of the mail addresses that may log in (exact
        # comparison); required
        "allowed_domains": [],
        "admins": [],
        "code_ttl_minutes": 15,
        "session_hours": 8,
        "remember_days": 180,  # "stay logged in": validity from last use
        # Limits for requesting a login code (not for entering it, not
        # for uploads):
        # per address; a lower value makes it easier to lock out a
        # person on purpose
        "max_codes_per_email_hour": 8,
        # per IP address; a whole computer room can sit behind one
        # address
        "max_codes_per_ip_hour": 200,
        # minimum time between two requests for the same address
        "code_cooldown_seconds": 30,
        # a login form sent faster than this after it was shown comes
        # from a bot
        "min_form_seconds": 2,
    },
    # details for the privacy statement (/privacy); real values only in
    # /etc/emptydiff/config.toml
    "privacy": {
        "controller": "",  # controller (name of the institution)
        "controller_address": "",  # postal address, multi-line
        "controller_email": "",  # contact of the controller
        "dpo_email": "",  # data protection officer
        # contact for questions about the procedure (lecturers)
        "contact_email": "",
        # optional: national supplement to Art. 6(1)(e) GDPR, e.g. the
        # state data protection act
        "legal_basis": "",
        "authority": "",  # competent supervisory authority (name)
        "authority_url": "",  # its website
        "imprint_url": "",  # optional: "Imprint" link in the footer
        # retention of the web server access logs (Apache/logrotate)
        "log_days": 7,
    },
    # sending login codes via Python (smtplib); host and sender are
    # required in production
    "mail": {
        # "smtp" or "simulate" (simulate only with EMPTYDIFF_DEV=1)
        "backend": "smtp",
        "host": "",  # SMTP server
        "port": 25,  # 25 (relay), 587 (starttls) or 465 (ssl)
        # "none" (plain text, e.g. internal relay), "starttls" or "ssl"
        "security": "none",
        "user": "",  # empty: no login at the server
        "password": "",
        "sender": "",  # sender address (From)
        "timeout_seconds": 15,
    },
    "grader": {
        # "bwrap" or "none" (none only with EMPTYDIFF_DEV=1)
        "sandbox": "bwrap",
        "timeout_seconds": 30,
        # submissions fetched per round; they are graded strictly one
        # after another
        "batch_size": 10,
    },
    # Programming languages: `enabled` lists the languages the server
    # offers (each needs its packages, see INSTALL.md); one section per
    # language holds the name of its interpreter and the words that mark
    # a submission as conspicuous (a hint for admins, never shown to
    # students: they have no business in a solution, but are the tools
    # for tampering with the check or reading it). The defaults come
    # from the language adapters.
    "languages": languages.defaults(),
    "limits": {
        "max_upload_bytes": 5_000_000,
        "max_attempts": 7,
        # size of the archive on restore (upload in the browser)
        "max_backup_bytes": 500_000_000,
    },
}


class ConfigError(Exception):
    pass


def is_dev():
    return os.environ.get("EMPTYDIFF_DEV") == "1"


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _validate(cfg):
    # Mail addresses and domains are compared in lower case everywhere,
    # so the configuration is lowered too.
    cfg["auth"]["allowed_domains"] = [
        str(d).strip().lower() for d in cfg["auth"]["allowed_domains"]
    ]
    cfg["auth"]["admins"] = [
        str(a).strip().lower() for a in cfg["auth"]["admins"]
    ]
    backend = cfg["mail"]["backend"]
    if backend not in ("smtp", "simulate"):
        raise ConfigError(f"mail.backend: unknown value {backend!r}")
    if backend == "simulate" and not is_dev():
        raise ConfigError(
            "mail.backend = 'simulate' is only allowed with EMPTYDIFF_DEV=1 "
            "(otherwise login codes would be publicly visible)"
        )
    language = cfg["ui"]["default_language"]
    if language not in i18n.languages():
        raise ConfigError(
            "ui.default_language: unknown language "
            f"{language!r} (available: {', '.join(i18n.languages())})"
        )
    security = cfg["mail"]["security"]
    if security not in ("none", "starttls", "ssl"):
        raise ConfigError(
            f"mail.security: unknown value {security!r} (none, "
            "starttls or ssl)"
        )
    limit = cfg["limits"]["max_attempts"]
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
    ):
        raise ConfigError(
            "limits.max_attempts: integer from 1 to 100 "
            f"expected, not {limit!r}"
        )
    backup = cfg["limits"]["max_backup_bytes"]
    if (
        not isinstance(backup, int)
        or isinstance(backup, bool)
        or not cfg["limits"]["max_upload_bytes"] <= backup <= 20 * 1024**3
    ):
        raise ConfigError(
            "limits.max_backup_bytes: integer of at least "
            "limits.max_upload_bytes expected"
        )
    batch = cfg["grader"]["batch_size"]
    if (
        not isinstance(batch, int)
        or isinstance(batch, bool)
        or not 1 <= batch <= 1000
    ):
        raise ConfigError(
            "grader.batch_size: integer from 1 to 1000 "
            f"expected, not {batch!r}"
        )
    for key, low, high in (
        ("max_codes_per_email_hour", 1, 1000),
        ("max_codes_per_ip_hour", 1, 100000),
        ("code_cooldown_seconds", 0, 3600),
        ("min_form_seconds", 0, 60),
    ):
        value = cfg["auth"][key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not low <= value <= high
        ):
            raise ConfigError(
                f"auth.{key}: integer from {low} to {high} "
                f"expected, not {value!r}"
            )
    section = cfg["languages"]
    enabled = section["enabled"]
    if (
        not isinstance(enabled, list)
        or not enabled
        or not all(isinstance(x, str) for x in enabled)
    ):
        raise ConfigError(
            "languages.enabled: non-empty list of language "
            "identifiers expected"
        )
    for language in enabled:
        if language not in languages.ids():
            raise ConfigError(
                f"languages.enabled: unknown language {language!r} "
                f"(known: {', '.join(languages.ids())})"
            )
    for language in languages.ids():
        entry = section[language]
        if (
            not isinstance(entry.get("command"), str)
            or not entry["command"].strip()
        ):
            raise ConfigError(
                f"languages.{language}.command: name of the "
                "interpreter expected"
            )
        words = entry.get("suspicious_words")
        if not isinstance(words, list) or not all(
            isinstance(w, str) and w.strip() for w in words
        ):
            raise ConfigError(
                f"languages.{language}.suspicious_words: list of "
                "non-empty strings expected"
            )
    sandbox = cfg["grader"]["sandbox"]
    if sandbox not in ("bwrap", "none"):
        raise ConfigError(f"grader.sandbox: unknown value {sandbox!r}")
    if sandbox == "none" and not is_dev():
        raise ConfigError(
            "grader.sandbox = 'none' is only allowed with EMPTYDIFF_DEV=1"
        )


def contact_hint(cfg):
    """Plain-text note " (address)" for messages that send students to
    their supervisors, or empty."""
    address = str(cfg["privacy"]["contact_email"]).strip()
    return f" ({address})" if address else ""


def uses_https(cfg):
    """True if the server is reached over https (`base_url`). Plain http
    is for closed networks only; then cookies cannot be `Secure`."""
    return str(cfg["base_url"]).lower().startswith("https://")


def check_production(cfg):
    """Required settings for running the web service; the check is
    skipped in dev mode."""
    if is_dev():
        return
    missing = []
    if not cfg["auth"]["allowed_domains"]:
        missing.append("auth.allowed_domains")
    if not cfg["auth"]["admins"]:
        missing.append("auth.admins")
    if cfg["mail"]["backend"] == "smtp":
        missing += [
            f"mail.{k}"
            for k in ("host", "sender")
            if not str(cfg["mail"][k]).strip()
        ]
    if missing:
        raise ConfigError(
            "Required settings are missing in the configuration: "
            + ", ".join(missing)
        )


def load(path=None):
    """Load the configuration. Order: argument, EMPTYDIFF_CONFIG,
    default path.

    In dev mode without a configuration file, config.example.toml is
    used.
    """
    path = path or os.environ.get("EMPTYDIFF_CONFIG") or DEFAULT_PATH
    if not os.path.exists(path) and is_dev():
        path = DEV_FALLBACK_PATH
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}") from None
    cfg = _merge(copy.deepcopy(DEFAULTS), data)
    _validate(cfg)
    return cfg
