# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression test: grade recorded student files against the testbenches
of the labs in the database and compare the verdict with the one
recorded with the files (DESIGN.md section 15).

Expects this directory layout (the lab directories carry the names of
labs in the database):
    <root>/<lab>/<sender>/RevNNN/{*.m, __passed_lab, ...}

Only files with the names of the lab's required files are used, as with
a real submission. Only shortened hashes of the sender directory names
are printed (no mail addresses).
"""

import hashlib
import os
import re
from dataclasses import dataclass, field

from . import grader, provided, submissions

MAX_FILE_BYTES = 1024 * 1024
REV_RE = re.compile(r"^Rev\d{3}$")


@dataclass
class Case:
    lab: str
    student: str  # shortened hash
    rev: str
    path: str


@dataclass
class Outcome:
    case: Case
    old_passed: bool
    new_passed: bool
    reason: str  # '', 'timeout', 'truncated', 'exit=N', 'unfinished'


@dataclass
class Report:
    outcomes: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)  # reason -> count

    @property
    def deviations(self):
        return [o for o in self.outcomes if o.old_passed != o.new_passed]


def anonymize(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]


def find_cases(root, labs=None):
    for lab in sorted(os.listdir(root)):
        lab_path = os.path.join(root, lab)
        if lab.startswith(".") or not os.path.isdir(lab_path):
            continue
        if labs and lab not in labs:
            continue
        for student in sorted(os.listdir(lab_path)):
            student_path = os.path.join(lab_path, student)
            if student.startswith(".") or not os.path.isdir(student_path):
                continue
            for rev in sorted(os.listdir(student_path)):
                rev_path = os.path.join(student_path, rev)
                if REV_RE.match(rev) and os.path.isdir(rev_path):
                    yield Case(lab, anonymize(student), rev, rev_path)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def load_case(case, required):
    """Read the files named in `required` and the recorded verdict.
    Returns (data, skip reason)."""
    files = {}
    for name in required:
        path = os.path.join(case.path, name)
        if os.path.isfile(path) and os.path.getsize(path) <= MAX_FILE_BYTES:
            files[name] = _read(path)
    if not files:
        return None, "no submission files"
    return {
        "files": files,
        "old_passed": os.path.exists(os.path.join(case.path, "__passed_lab")),
    }, None


def run(cfg, conn, root, labs=None, limit=None, progress=None):
    """Run all revisions below `root`, one after the other.
    `progress(done, total)` is called now and then."""
    report = Report()
    cases = []
    lab_rows = {}
    for case in find_cases(root, labs):
        if case.lab not in lab_rows:
            row = conn.execute(
                "SELECT * FROM lab WHERE name = ?", (case.lab,)
            ).fetchone()
            lab_rows[case.lab] = (
                row,
                (
                    provided.files(row["language"], row["provided"])
                    if row
                    else {}
                ),
            )
        row, supplied = lab_rows[case.lab]
        if row is None:
            report.skipped["lab not in database"] = (
                report.skipped.get("lab not in database", 0) + 1
            )
            continue
        data, reason = load_case(case, submissions.required_files(row))
        if reason:
            report.skipped[reason] = report.skipped.get(reason, 0) + 1
            continue
        cases.append((case, data))
        if limit and len(cases) >= limit:
            break

    def work(item):
        case, data = item
        row, supplied = lab_rows[case.lab]
        result = grader.grade(
            cfg,
            row["testbench"],
            data["files"],
            supplied,
            helpers=row["helpers"],
            language=row["language"],
        )
        reason = (
            "timeout"
            if result.timed_out
            else (
                "truncated"
                if result.truncated
                else (
                    f"exit={result.exit_code}"
                    if result.exit_code != 0
                    else "unfinished" if not result.finished else ""
                )
            )
        )
        return Outcome(case, data["old_passed"], result.passed, reason)

    for i, item in enumerate(cases, start=1):
        report.outcomes.append(work(item))
        if progress and (i % 250 == 0 or i == len(cases)):
            progress(i, len(cases))
    return report
