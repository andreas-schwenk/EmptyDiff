# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Report for students: hints, the recorded checks (ticks), the
program's own output and the error output of the interpreter.

Everything is plain text; the templates show it escaped.
"""

import json
from dataclasses import dataclass, field

from . import i18n

MAX_REPORT_CHARS = 64 * 1024
# Stored (language-neutral) note at the end of an output that the worker
# shortened; shown translated.
CLIP_MARK = "[... truncated ...]"


@dataclass
class Report:
    internal_error: str = ""  # if set, nothing else is shown
    notes: list = field(
        default_factory=list
    )  # hints: time limit, too much output, aborted run
    checks: list = field(default_factory=list)  # [(ok, text)]
    output: str = ""  # what the student's program printed
    errors: str = ""  # error output of the interpreter


def _localize_clip(text, lang):
    if text.endswith(CLIP_MARK):
        return text[: -len(CLIP_MARK)] + i18n.translate(lang, "report.clipped")
    return text


def _shorten(text, lang):
    if len(text) > MAX_REPORT_CHARS:
        return (
            text[:MAX_REPORT_CHARS]
            + "\n"
            + i18n.translate(lang, "report.shortened")
        )
    return text


def checks_of(sub):
    """The recorded checks of a submission row as [(ok, text)]."""
    try:
        return [
            (bool(ok), str(text))
            for ok, text in json.loads(sub["checks"] or "[]")
        ]
    except (ValueError, TypeError):
        return []


def build(sub, lang=i18n.OPERATOR_LANGUAGE, contact=""):
    """Report for a fully evaluated submission (row of the table
    submission) in the language `lang`.

    `contact` is a plain-text note like " (address)" appended where
    students are sent to their supervisors.
    """
    if sub["status"] == "error":
        return Report(
            internal_error=i18n.translate(
                lang, "report.internal_error", contact=contact
            )
        )
    report = Report(checks=checks_of(sub))
    if sub["timed_out"]:
        report.notes.append(i18n.translate(lang, "report.timeout"))
        return report
    if sub["truncated"]:
        report.notes.append(i18n.translate(lang, "report.truncated"))
    elif not sub["finished"]:
        report.notes.append(i18n.translate(lang, "report.aborted"))
    report.output = _shorten(
        _localize_clip((sub["log"] or "").strip("\n"), lang), lang
    )
    report.errors = _shorten(
        _localize_clip((sub["error_log"] or "").strip("\n"), lang), lang
    )
    return report


def raw_output(sub, lang=i18n.OPERATOR_LANGUAGE):
    """(stdout, stderr) of the run as stored, for lecturers.

    Unlike the report this is also there when the run did not finish,
    timed out or failed inside the system, and when students only get
    a receipt.
    """
    return tuple(
        _shorten(_localize_clip((sub[column] or "").strip("\n"), lang), lang)
        for column in ("log", "error_log")
    )


def as_text(report, lang=i18n.OPERATOR_LANGUAGE):
    """The report as plain text (for the archive of a lab)."""
    if report.internal_error:
        return report.internal_error
    parts = list(report.notes)
    if report.checks:
        parts.append(
            "\n".join(
                f"[{'OK' if ok else 'FAIL'}] {text}"
                for ok, text in report.checks
            )
        )
    if report.output.strip():
        parts.append(
            i18n.translate(lang, "report.output") + ":\n" + report.output
        )
    if report.errors.strip():
        parts.append(
            i18n.translate(lang, "report.errors") + ":\n" + report.errors
        )
    return "\n\n".join(parts)
