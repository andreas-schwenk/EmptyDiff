# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Credit list (DESIGN.md section 11): student x lab of a course, the
last submission counts."""

import csv
import io

from . import i18n

STATES = ("none", "pending", "error", "received", "pass", "fail")


def labels(lang):
    """Label per state in the language `lang` (empty for 'none')."""
    return {
        state: i18n.translate(lang, f"credit.state.{state}")
        for state in STATES
    }


def state(lab, last):
    """State of a submission (`last`, may be None) for a lab."""
    if last is None:
        return "none"
    if last["status"] in ("queued", "running"):
        return "pending"
    if last["status"] == "error":
        return "error"
    if not lab["show_report"]:
        return "received"  # is graded manually
    return "pass" if last["passed"] and last["complete"] else "fail"


def matrix(conn, course):
    """Return (labs, rows); rows are {email, cells, passed} per student.

    A cell is {state, late, attempts, sub_id}. Only students with at
    least one submission for a lab of the course are considered.
    """
    labs = conn.execute(
        "SELECT * FROM lab WHERE course = ? ORDER BY name", (course,)
    ).fetchall()
    names = [lab["name"] for lab in labs]
    if not names:
        return labs, []
    marks = ",".join("?" * len(names))
    last = {}
    count = {}
    for s in conn.execute(
        f"SELECT * FROM submission WHERE lab IN ({marks}) ORDER BY attempt",
        names,
    ):
        last[(s["email"], s["lab"])] = s  # ascending by lab: the last one wins
        count[(s["email"], s["lab"])] = (
            count.get((s["email"], s["lab"]), 0) + 1
        )
    rows = []
    for email in sorted({e for e, _ in last}):
        cells = []
        for lab in labs:
            sub = last.get((email, lab["name"]))
            cells.append(
                {
                    "state": state(lab, sub),
                    "late": bool(sub and sub["late"]),
                    "attempts": count.get((email, lab["name"]), 0),
                    "sub_id": sub["id"] if sub else None,
                }
            )
        rows.append(
            {
                "email": email,
                "cells": cells,
                "passed": sum(c["state"] == "pass" for c in cells),
            }
        )
    return labs, rows


def to_csv(labs, rows, lang=i18n.OPERATOR_LANGUAGE):
    """CSV text (UTF-8, comma). Cells contain only fixed words of the
    language `lang`, no user input except addresses."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["email"]
        + [lab["name"] for lab in labs]
        + [i18n.translate(lang, "credit.csv_passed")]
    )
    names = labels(lang)
    for row in rows:
        cells = []
        for c in row["cells"]:
            text = names[c["state"]]
            if c["late"] and text:
                text += i18n.translate(lang, "credit.csv_late")
            cells.append(text)
        writer.writerow([row["email"]] + cells + [row["passed"]])
    return out.getvalue()
