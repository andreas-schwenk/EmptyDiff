# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lab and course management (DESIGN.md section 10): create, edit,
activate, clone, delete.

Standard library only. Errors with a message readable by lecturers are
LabError.
"""

import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta

from . import grader, i18n, languages, provided, submissions, util

NAME_RE = re.compile(r"^[a-z]{1,8}[0-9]{2}-[A-Za-z0-9]{1,16}$")
COURSE_RE = re.compile(r"^[a-z]{1,8}[0-9]{2}$")
IDENT_RE = re.compile(
    r"^[A-Za-z0-9]{1,16}$"
)  # identifier of a lab within its course, e.g. "1a"
MAX_TESTBENCH_CHARS = 200_000
MAX_HELPERS_CHARS = 200_000
MAX_DESCRIPTION_CHARS = 20_000
DUE_FORMAT = "%Y-%m-%dT%H:%M"  # value of <input type="datetime-local">


class LabError(i18n.LocalizedError):
    """Error with a message that lecturers can read."""


def course_of(name):
    return name.split("-")[0]


# ---------- Deadline ----------


def parse_due(text):
    """Local time (Europe/Berlin, minute resolution) -> UTC timestamp.

    The deadline holds until the end of the given minute (:59), so that
    '23:59' lasts until 23:59:59.
    """
    try:
        local = datetime.strptime((text or "").strip(), DUE_FORMAT)
    except ValueError:
        raise LabError("err.lab.due_format") from None
    local = local.replace(second=59, tzinfo=util.BERLIN)
    return util.to_iso(local)


def due_for_input(iso):
    """UTC timestamp -> value for <input type="datetime-local"> (local
    time)."""
    return util.from_iso(iso).astimezone(util.BERLIN).strftime(DUE_FORMAT)


# ---------- Validating input ----------


def parse_required(text, language=languages.DEFAULT):
    """Required files: one per line (commas are allowed as well).
    Returns the list."""
    reserved = languages.get(language).testbench_file
    names = []
    for part in re.split(r"[\n,]", text or ""):
        part = part.strip()
        if not part:
            continue
        if not grader.FILENAME_RE.match(part):
            raise LabError("err.lab.required_name", name=part)
        if part == reserved:
            raise LabError("err.lab.required_reserved", name=part)
        if part not in names:
            names.append(part)
    return names


def _normalize_text(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def clean_language(value, enabled, current=None):
    """The language of a lab from the form value: one of the languages
    the server offers, or the one the lab already has (a lab keeps its
    language even if the server stopped offering it). Without a value
    the current or the default.
    """
    language = (value or current or languages.DEFAULT).strip()
    if language != current and language not in enabled:
        raise LabError("err.lab.language", language=language)
    languages.get(
        language
    )  # a language that is not known at all cannot come through
    return language


def clean_fields(form, enabled=(languages.DEFAULT,), current=None):
    """Check the form fields of a lab and return the DB values (without
    the name).

    `enabled` are the languages the server offers, `current` the
    language of the lab that is edited (None: new lab).
    """
    language = clean_language(form.get("language"), enabled, current)
    adapter = languages.get(language)
    description = _normalize_text(form.get("description")).strip()
    testbench = _normalize_text(form.get("testbench")).rstrip() + "\n"
    helpers = _normalize_text(form.get("helpers")).strip()
    helpers = helpers + "\n" if helpers else ""
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise LabError("err.lab.desc_long")
    if not testbench.strip():
        raise LabError("err.lab.tb_empty")
    if len(testbench) > MAX_TESTBENCH_CHARS:
        raise LabError("err.lab.tb_long")
    if len(helpers) > MAX_HELPERS_CHARS:
        raise LabError("err.lab.helpers_long")
    try:
        adapter.check_testbench(testbench)
    except languages.TestbenchError as e:
        raise LabError(e.key, **e.params) from None
    library = _normalize_text(form.get("provided")).strip()
    library = library + "\n" if library else ""
    if len(library) > provided.MAX_CHARS:
        raise LabError("err.lab.provided_long")
    try:
        provided.split(
            language, library
        )  # only to check it: every function must be usable as a file
    except provided.ProvidedError as e:
        raise LabError(e.key, **e.params) from None
    required = parse_required(form.get("required_files"), language)
    if not required:
        raise LabError("err.lab.required_empty")
    return {
        "language": language,
        "description": description,
        "due_at": parse_due(form.get("due_at")),
        "required_files": "\n".join(required),
        "show_report": int(bool(form.get("show_report"))),
        "testbench": testbench,
        "helpers": helpers,
        "provided": library,
    }


def warnings_for(testbench, helpers="", language=languages.DEFAULT):
    """Keys of the hints about the testbench; they do not block
    saving."""
    if not languages.get(language).has_check(testbench + "\n" + helpers):
        return ["warn.tb_no_check"]
    return []


# ---------- Reading ----------


def get(conn, name):
    return conn.execute("SELECT * FROM lab WHERE name = ?", (name,)).fetchone()


def course_exists(conn, course):
    return (
        conn.execute(
            "SELECT 1 FROM course WHERE name = ?", (course,)
        ).fetchone()
        is not None
    )


def overview(conn, exclude=()):
    """Courses (newest first, also empty ones) with their labs and
    counters for the admin overview.

    Per lab: `submissions` (all of them) and `students`, the number of
    students who submitted. Per course: `students`, the number of
    students who submitted anything in one of its labs, the base of the
    ratio shown per lab. The addresses in `exclude` (the lecturers, who
    try labs out) are not counted as students.
    """
    marks = ",".join("?" * len(exclude))
    where = f"WHERE email NOT IN ({marks})" if exclude else ""
    stats = {
        r["lab"]: r
        for r in conn.execute(
            "SELECT lab, COUNT(*) AS submissions FROM submission "
            "GROUP BY lab"
        )
    }
    students = {
        r["lab"]: r["students"]
        for r in conn.execute(
            "SELECT lab, COUNT(DISTINCT email) AS students FROM "
            f"submission {where} GROUP BY lab",
            tuple(exclude),
        )
    }
    course_students = {
        r["course"]: r["students"]
        for r in conn.execute(
            "SELECT lab.course AS course, COUNT(DISTINCT "
            "submission.email) AS students FROM submission JOIN lab "
            "ON lab.name = submission.lab "
            + (f"WHERE submission.email NOT IN ({marks}) " if exclude else "")
            + "GROUP BY lab.course",
            tuple(exclude),
        )
    }
    courses = {r["name"]: [] for r in conn.execute("SELECT name FROM course")}
    for lab in conn.execute("SELECT * FROM lab ORDER BY name"):
        st = stats.get(lab["name"])
        entry = {
            "lab": lab,
            "students": students.get(lab["name"], 0),
            "submissions": st["submissions"] if st else 0,
        }
        courses.setdefault(lab["course"], []).append(entry)
    result = []
    for course in sorted(courses, key=_course_sort_key, reverse=True):
        labs = courses[course]
        result.append(
            {
                "course": course,
                "labs": labs,
                "active": sum(e["lab"]["active"] for e in labs),
                "students": course_students.get(course, 0),
            }
        )
    return result


def _course_sort_key(course):
    """'am26' before 'am25'; for the same year number by prefix."""
    return (course[-2:], course[:-2])


# ---------- Writing ----------


def create_course(conn, name):
    """Create an empty course. Returns its name."""
    name = (name or "").strip()
    if not COURSE_RE.match(name):
        raise LabError("err.lab.course_format")
    try:
        conn.execute("INSERT INTO course (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        conn.rollback()
        raise LabError("err.lab.course_exists", name=name) from None
    conn.commit()
    return name


def delete_course(conn, name):
    """Delete a course. Only an empty course can be deleted; labs are
    deleted one by one."""
    if not course_exists(conn, name):
        raise LabError("err.lab.no_course")
    if conn.execute("SELECT 1 FROM lab WHERE course = ?", (name,)).fetchone():
        raise LabError("err.lab.course_not_empty")
    conn.execute("DELETE FROM course WHERE name = ?", (name,))
    conn.commit()


def create(conn, course, ident, fields):
    """Create the lab `<course>-<ident>` (e.g. am26 + 1a) in an existing
    course, hidden. Returns its name.

    Visibility is only changed with `set_active` (the switch in the lab
    list), never by the lab form.
    """
    if not course_exists(conn, course):
        raise LabError("err.lab.no_course")
    ident = (ident or "").strip()
    if not IDENT_RE.match(ident):
        raise LabError("err.lab.ident_format")
    name = f"{course}-{ident}"
    try:
        conn.execute(
            "INSERT INTO lab (name, course, language, "
            "description, due_at, required_files, "
            "show_report, testbench, helpers, provided, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                name,
                course_of(name),
                fields["language"],
                fields["description"],
                fields["due_at"],
                fields["required_files"],
                fields["show_report"],
                fields["testbench"],
                fields["helpers"],
                fields["provided"],
            ),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        raise LabError("err.lab.exists", name=name) from None
    conn.commit()
    return name


def update(conn, name, fields):
    current = get(conn, name)
    if (
        current is not None
        and fields["language"] != current["language"]
        and conn.execute(
            "SELECT 1 FROM submission WHERE lab = ?", (name,)
        ).fetchone()
    ):
        raise LabError(
            "err.lab.language_locked"
        )  # submissions and testbench have to match
    cur = conn.execute(
        "UPDATE lab SET language = ?, description = ?, "
        "due_at = ?, required_files = ?, "
        "show_report = ?, testbench = ?, helpers = ?, "
        "provided = ? WHERE name = ?",
        (
            fields["language"],
            fields["description"],
            fields["due_at"],
            fields["required_files"],
            fields["show_report"],
            fields["testbench"],
            fields["helpers"],
            fields["provided"],
            name,
        ),
    )
    if cur.rowcount != 1:
        raise LabError("err.lab.no_lab")
    # a moved deadline changes what counts as late
    submissions.refresh_late(conn, lab_name=name)
    conn.commit()


def set_active(conn, name, active):
    cur = conn.execute(
        "UPDATE lab SET active = ? WHERE name = ?", (int(bool(active)), name)
    )
    conn.commit()
    if cur.rowcount != 1:
        raise LabError("err.lab.no_lab")


def delete(conn, cfg, name):
    """Delete a lab with all submissions (rows and files). Returns the
    number of submissions.

    Admins are explicitly allowed to do this (e.g. for test labs); the
    confirmation is in the interface. If a check is running right now,
    it is refused (as when deleting a single submission).
    """
    if get(conn, name) is None:
        raise LabError("err.lab.no_lab")
    if conn.execute(
        "SELECT 1 FROM submission WHERE lab = ? AND status = 'running'",
        (name,),
    ).fetchone():
        raise LabError("err.lab.delete_running")
    count = conn.execute(
        "SELECT COUNT(*) FROM submission WHERE lab = ?", (name,)
    ).fetchone()[0]
    conn.execute("DELETE FROM submission WHERE lab = ?", (name,))
    conn.execute("DELETE FROM lab WHERE name = ?", (name,))
    conn.commit()
    shutil.rmtree(
        os.path.join(cfg["data_dir"], "submissions", name), ignore_errors=True
    )
    return count


def shift_due(iso, days):
    """Shift a deadline by whole days and keep the local time of day
    (daylight-saving safe)."""
    local = util.from_iso(iso).astimezone(util.BERLIN).replace(
        tzinfo=None
    ) + timedelta(days=days)
    return util.to_iso(local.replace(tzinfo=util.BERLIN))


def clone_course(conn, src, dst, shift_days=0):
    """Copy all labs of course `src` into the new course `dst`
    (inactive, deadlines shifted)."""
    if not COURSE_RE.match(dst or ""):
        raise LabError("err.lab.course_format")
    labs = conn.execute(
        "SELECT * FROM lab WHERE course = ? ORDER BY name", (src,)
    ).fetchall()
    if not labs:
        raise LabError("err.lab.src_empty")
    if course_exists(conn, dst):
        raise LabError("err.lab.course_exists", name=dst)
    new_names = []
    try:
        conn.execute("INSERT INTO course (name) VALUES (?)", (dst,))
        for lab in labs:
            new_name = dst + lab["name"][len(src) :]
            conn.execute(
                "INSERT INTO lab (name, course, language, "
                "description, due_at, required_files, "
                "show_report, testbench, helpers, provided, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    new_name,
                    dst,
                    lab["language"],
                    lab["description"],
                    shift_due(lab["due_at"], shift_days),
                    lab["required_files"],
                    lab["show_report"],
                    lab["testbench"],
                    lab["helpers"],
                    lab["provided"],
                ),
            )
            new_names.append(new_name)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return new_names


# ---------- Managing submissions ----------


def submissions_of_lab(conn, name):
    return conn.execute(
        "SELECT * FROM submission WHERE lab = ? ORDER BY email, attempt",
        (name,),
    ).fetchall()


def delete_submission(conn, cfg, sub_id):
    """Delete a submission with its files. If its check is still
    running, it is refused."""
    sub = submissions.get(conn, sub_id)
    if sub is None:
        raise LabError("err.lab.no_sub")
    if sub["status"] == "running":
        raise LabError("err.lab.sub_running")
    conn.execute("DELETE FROM submission WHERE id = ?", (sub_id,))
    conn.commit()
    shutil.rmtree(
        submissions.submission_dir(
            cfg, sub["lab"], sub["email"], sub["attempt"]
        ),
        ignore_errors=True,
    )
    return sub


def regrade_submission(conn, sub_id):
    """Put a submission back into the grading queue (e.g. after fixing
    the testbench)."""
    sub = submissions.get(conn, sub_id)
    if sub is None:
        raise LabError("err.lab.no_sub")
    if sub["status"] in ("queued", "running"):
        raise LabError("err.lab.sub_pending")
    conn.execute(
        "UPDATE submission SET status = 'queued', passed = "
        "NULL, log = NULL, error_log = NULL, timed_out = 0, "
        "truncated = 0, checks = '[]', finished = 0, flags = '' WHERE id = ?",
        (sub_id,),
    )
    submissions.refresh_late(conn, sub_id=sub_id)  # current deadline
    conn.commit()
