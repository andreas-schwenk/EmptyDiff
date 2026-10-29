# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""SQLite access and schema. Standard library only, hand-written SQL.

Migrations: PRAGMA user_version and the list MIGRATIONS (index + 1 =
version).
"""

import os
import re
import sqlite3

_SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "..", "schema.sql")


def _migration_v1(conn):
    with open(_SCHEMA_FILE, encoding="utf-8") as f:
        conn.executescript(f.read())


def _migration_v2(conn):
    """Result details of the evaluation and missing required files."""
    for statement in (
        "ALTER TABLE submission ADD COLUMN missing_files "
        "TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE submission ADD COLUMN exit_code INTEGER",
        "ALTER TABLE submission ADD COLUMN timed_out INTEGER "
        "NOT NULL DEFAULT 0",
        "ALTER TABLE submission ADD COLUMN truncated INTEGER "
        "NOT NULL DEFAULT 0",
        "ALTER TABLE submission ADD COLUMN duration REAL",
        "ALTER TABLE submission ADD COLUMN finished_at TEXT",
    ):
        conn.execute(statement)


def _migration_v3(conn):
    """Additional attempts for individual students (dropped, see v5;
    kept for existing databases)."""
    conn.execute(
        "CREATE TABLE attempt_grant ("
        " lab TEXT NOT NULL REFERENCES lab(name) ON DELETE CASCADE,"
        " email TEXT NOT NULL,"
        " extra INTEGER NOT NULL CHECK (extra > 0),"
        " granted_at TEXT NOT NULL,"
        " PRIMARY KEY (lab, email))"
    )


def _migration_v4(conn):
    """Remember-me ("stay logged in") tokens that can be revoked on the
    server (only the hash is stored)."""
    conn.execute(
        "CREATE TABLE remember_token ("
        " token_hash TEXT PRIMARY KEY,"
        " email TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX remember_token_expires ON remember_token(expires_at)"
    )


def _migration_v5(conn):
    """Additional attempts per student were dropped (deleting
    submissions resets the counter)."""
    conn.execute("DROP TABLE IF EXISTS attempt_grant")


def _migration_v6(conn):
    """The attempt limit is the same for all labs and set in the
    configuration (limits.max_attempts)."""
    conn.execute("ALTER TABLE lab DROP COLUMN max_attempts")


def _migration_v7(conn):
    """Small key-value table for operations (worker heartbeat, last
    purge)."""
    conn.execute(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('created_at', "
        "strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
    )


def _migration_v8(conn):
    """Clean-up: the simulated mail history (dev only) and three columns
    that were never read are dropped."""
    conn.execute("DROP TABLE IF EXISTS outbox")
    for column in ("exit_code", "duration", "finished_at"):
        conn.execute(f"ALTER TABLE submission DROP COLUMN {column}")


def _migration_v9(conn):
    """The "note" field is dropped: no lab used it, the description is
    enough."""
    conn.execute("ALTER TABLE lab DROP COLUMN note")


def _migration_v10(conn):
    """Courses are their own objects (they can exist without labs). A
    trigger keeps every lab's course present."""
    conn.execute("CREATE TABLE course (name TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO course (name) SELECT DISTINCT course FROM lab")
    conn.execute(
        "CREATE TRIGGER lab_course AFTER INSERT ON lab "
        "BEGIN INSERT OR IGNORE INTO course (name) VALUES (NEW.course); END"
    )


def _migration_v11(conn):
    """Testbench = body + helper functions; structured check results,
    run state and flags per submission."""
    for statement in (
        "ALTER TABLE lab ADD COLUMN helpers TEXT NOT NULL DEFAULT ''",
        # JSON: [[ok, text], ...]
        "ALTER TABLE submission ADD COLUMN checks TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE submission ADD COLUMN finished INTEGER "
        "NOT NULL DEFAULT 0",
        # conspicuous words, one per line
        "ALTER TABLE submission ADD COLUMN flags TEXT NOT NULL DEFAULT ''",
        "UPDATE submission SET finished = 1 WHERE status = 'done'",
    ):
        conn.execute(statement)


def _migration_v12(conn):
    """Provided files become provided functions: one text per lab (every
    function is written to its own file).

    Files of the old table that are not exactly one function named like
    the file (for example data files) are dropped.
    """
    conn.execute(
        "ALTER TABLE lab ADD COLUMN provided TEXT NOT NULL DEFAULT ''"
    )
    header = re.compile(r"^[ \t]*function\b[^\n]*", re.MULTILINE)
    name_of = re.compile(
        r"^\s*(?:(?:\[[^\]\n]*\]|[A-Za-z_]\w*)\s*=\s*)?([A-Za-z_]\w*)"
    )
    texts = {}
    for row in conn.execute(
        "SELECT lab, name, content FROM lab_file ORDER BY lab, name"
    ).fetchall():
        try:
            text = bytes(row["content"]).decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
        found = header.findall(text)
        match = (
            name_of.match(found[0].split("function", 1)[1])
            if len(found) == 1
            else None
        )
        if (
            row["name"].endswith(".m")
            and match
            and match.group(1) == row["name"][:-2]
        ):
            texts.setdefault(row["lab"], []).append(text.strip() + "\n")
    for lab, parts in texts.items():
        conn.execute(
            "UPDATE lab SET provided = ? WHERE name = ?",
            ("\n".join(parts), lab),
        )
    conn.execute("DROP TABLE lab_file")


def _migration_v13(conn):
    """Every lab has a programming language (identifier of a language
    adapter); existing labs are Octave labs."""
    conn.execute(
        "ALTER TABLE lab ADD COLUMN language TEXT NOT NULL DEFAULT 'octave'"
    )


MIGRATIONS = [
    _migration_v1,
    _migration_v2,
    _migration_v3,
    _migration_v4,
    _migration_v5,
    _migration_v6,
    _migration_v7,
    _migration_v8,
    _migration_v9,
    _migration_v10,
    _migration_v11,
    _migration_v12,
    _migration_v13,
]


def meta_get(conn, key):
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON "
        "CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def path_for(cfg):
    return os.path.join(cfg["data_dir"], "emptydiff.sqlite")


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path):
    """Create the database or migrate it to the current version."""
    conn = connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, migration in enumerate(MIGRATIONS[version:], start=version + 1):
        migration(conn)
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()
    return conn
