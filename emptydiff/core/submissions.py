# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Submissions: validation, storage and queries (DESIGN.md section
7)."""

import hashlib
import os
import shutil
import sqlite3

from . import config, db, grader, i18n, languages, provided, util


class SubmissionError(i18n.LocalizedError):
    """Error with a message that students can read (and that can be
    translated)."""


def user_hash(email):
    """Pseudonymized directory name; the mapping to the address exists
    only in the DB."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


def submission_dir(cfg, lab, email, attempt):
    return os.path.join(
        cfg["data_dir"], "submissions", lab, user_hash(email), f"{attempt:03d}"
    )


def required_files(lab):
    return [
        line.strip()
        for line in lab["required_files"].splitlines()
        if line.strip()
    ]


def submit(conn, cfg, lab_name, email, files, now=None, preview=False):
    """Store a submission and put it into the queue. Returns the ID.

    Only files with the names of the required files are accepted. After
    the deadline a submission is accepted anyway and marked as late.
    Missing required files mark it as incomplete (provided functions
    count as present).

    `preview` is for lecturers: they may submit to a lab that is still
    hidden from students. Everything else is the same as for students
    (limit of attempts, checks, report).
    """
    now = now or util.utcnow()
    lab = conn.execute(
        "SELECT * FROM lab WHERE name = ?", (lab_name,)
    ).fetchone()
    if lab is None or not (lab["active"] or preview):
        raise SubmissionError("err.submit.unavailable")
    if lab["language"] not in languages.enabled(cfg):
        raise SubmissionError(
            "err.submit.language", contact=config.contact_hint(cfg)
        )  # the server does not offer the language (any more)
    if not files:
        raise SubmissionError("err.submit.no_files")
    bad = sorted(n for n in files if not grader.FILENAME_RE.match(n))
    if bad:
        raise SubmissionError("err.submit.bad_names", names=", ".join(bad))
    allowed = required_files(lab)
    unexpected = sorted(n for n in files if n not in allowed)
    if unexpected:
        raise SubmissionError(
            "err.submit.unexpected_files",
            names=", ".join(unexpected),
            allowed=", ".join(allowed),
        )
    limit = cfg["limits"]["max_upload_bytes"]
    if sum(len(c) for c in files.values()) > limit:
        raise SubmissionError("err.submit.too_large", mb=limit // 1_000_000)

    if not conn.in_transaction:
        # take the write lock before the check: no double submission
        # through a race
        conn.execute("BEGIN IMMEDIATE")
    try:
        if db.meta_get(conn, "maintenance"):
            raise SubmissionError("err.submit.maintenance")
        used, last = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(attempt), 0) FROM "
            "submission WHERE lab = ? AND email = ?",
            (lab_name, email),
        ).fetchone()
        limit = cfg["limits"]["max_attempts"]
        # what exists is counted: deleting by supervisors gives attempts
        # back
        if used >= limit:
            raise SubmissionError(
                "err.submit.max_reached",
                max=limit,
                contact=config.contact_hint(cfg),
            )
        pending = conn.execute(
            "SELECT 1 FROM submission WHERE lab = ? AND email = "
            "? AND status IN ('queued', 'running')",
            (lab_name, email),
        ).fetchone()
        if pending:
            raise SubmissionError("err.submit.pending")

        supplied = provided.names(lab["language"], lab["provided"])
        missing = [
            r
            for r in required_files(lab)
            if r not in files and r not in supplied
        ]
        attempt = last + 1
        directory = submission_dir(cfg, lab_name, email, attempt)
        os.makedirs(directory, exist_ok=True)
        try:
            for name, content in files.items():
                with open(os.path.join(directory, name), "wb") as f:
                    f.write(content)
            cur = conn.execute(
                "INSERT INTO submission (lab, email, attempt, "
                "created_at, late, complete, missing_files) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    lab_name,
                    email,
                    attempt,
                    util.to_iso(now),
                    int(util.to_iso(now) > lab["due_at"]),
                    int(not missing),
                    "\n".join(missing),
                ),
            )
        except (OSError, sqlite3.IntegrityError):
            shutil.rmtree(directory, ignore_errors=True)
            raise SubmissionError("err.submit.store_failed")
        conn.commit()
        return cur.lastrowid
    except BaseException:
        conn.rollback()
        raise


def refresh_late(conn, lab_name=None, sub_id=None):
    """Set `late` from the current deadline of the lab: a deadline that
    is moved changes what counts as late (all submissions, those of one
    lab or one submission). The caller commits."""
    sql = (
        "UPDATE submission SET late = (created_at > "
        "(SELECT due_at FROM lab WHERE lab.name = submission.lab))"
    )
    if sub_id is not None:
        conn.execute(sql + " WHERE id = ?", (sub_id,))
    elif lab_name is not None:
        conn.execute(sql + " WHERE lab = ?", (lab_name,))
    else:
        conn.execute(sql)


def load_files(cfg, sub):
    directory = submission_dir(cfg, sub["lab"], sub["email"], sub["attempt"])
    files = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and grader.FILENAME_RE.match(name):
            with open(path, "rb") as f:
                files[name] = f.read()
    return files


def get(conn, sub_id):
    return conn.execute(
        "SELECT * FROM submission WHERE id = ?", (sub_id,)
    ).fetchone()


def list_for_user(conn, email, lab=None):
    sql = "SELECT * FROM submission WHERE email = ?"
    args = [email]
    if lab:
        sql += " AND lab = ?"
        args.append(lab)
    return conn.execute(sql + " ORDER BY lab, attempt", args).fetchall()


def overview_for_user(conn, email, all_labs=False):
    """Per lab (active or with a submission): number of attempts and
    state of the last submission.

    Lecturers (`all_labs`) get every lab, also the hidden ones, so that
    they can try them out like a student.
    """
    if all_labs:
        labs = conn.execute("SELECT * FROM lab ORDER BY name").fetchall()
    else:
        labs = conn.execute(
            "SELECT * FROM lab WHERE active = 1 OR name IN "
            "(SELECT lab FROM submission WHERE email = ?) ORDER "
            "BY name",
            (email,),
        ).fetchall()
    rows = []
    for lab in labs:
        subs = list_for_user(conn, email, lab["name"])
        rows.append(
            {
                "lab": lab,
                "attempts": len(subs),
                "last": subs[-1] if subs else None,
            }
        )
    return rows
