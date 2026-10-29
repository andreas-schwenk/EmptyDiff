# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

import json
import unittest

from emptydiff.core import i18n, report


def build(row):
    return report.build(row, "DE")  # these tests check the German texts


def sub(**kw):
    base = dict(
        status="done",
        timed_out=0,
        truncated=0,
        finished=1,
        checks="[]",
        log="",
        error_log="",
    )
    base.update(kw)
    if not isinstance(base["checks"], str):
        base["checks"] = json.dumps(base["checks"])
    return base


class ReportTest(unittest.TestCase):
    def test_checks_are_listed_in_order(self):
        r = build(sub(checks=[[1, "a"], [0, "b"]]))
        self.assertEqual(r.checks, [(True, "a"), (False, "b")])
        self.assertEqual(
            (r.notes, r.output, r.errors, r.internal_error), ([], "", "", "")
        )

    def test_aborted_run_shows_hint_and_octave_error(self):
        r = build(
            sub(
                finished=0, checks=[[1, "a"]], error_log="error: parse error\n"
            )
        )
        self.assertEqual(len(r.notes), 1)
        self.assertIn("nicht vollständig ausgeführt", r.notes[0])
        self.assertEqual(r.checks, [(True, "a")])
        self.assertEqual(r.errors, "error: parse error")

    def test_the_programs_own_output_is_kept_apart(self):
        r = build(sub(checks=[[1, "a"]], log="[OK] gefaelscht\nhallo\n"))
        self.assertEqual(r.checks, [(True, "a")])
        self.assertEqual(r.output, "[OK] gefaelscht\nhallo")

    def test_timeout_and_truncation(self):
        r = build(sub(timed_out=1, finished=0, error_log="egal"))
        self.assertIn("Endlosschleifen", r.notes[0])
        self.assertEqual(r.errors, "")
        r = build(sub(truncated=1, finished=0))
        self.assertIn("umfangreiche Ausgabe", r.notes[0])
        self.assertEqual(len(r.notes), 1)

    def test_internal_error_hides_details(self):
        r = build(
            sub(status="error", error_log="GraderError: geheimer Pfad /var/x")
        )
        self.assertEqual(
            r.internal_error,
            i18n.translate("DE", "report.internal_error", contact=""),
        )
        self.assertEqual((r.checks, r.errors), ([], ""))

    def test_raw_output_is_there_even_when_the_report_hides_it(self):
        row = sub(
            status="error",
            log="",
            error_log="GraderError: geheimer Pfad /var/x",
            timed_out=1,
        )
        self.assertEqual(
            report.raw_output(row),
            ("", "GraderError: geheimer Pfad /var/x"),
        )
        row = sub(log="hallo\n", error_log="error: x\n[... truncated ...]")
        out, err = report.raw_output(row, "DE")
        self.assertEqual(out, "hallo")
        self.assertTrue(err.startswith("error: x"))
        self.assertNotIn("[... truncated ...]", err)

    def test_long_output_is_clipped(self):
        r = build(sub(log="x\n" * report.MAX_REPORT_CHARS))
        self.assertTrue(r.output.endswith("[... Bericht gekürzt ...]"))
        self.assertLess(len(r.output), report.MAX_REPORT_CHARS + 100)

    def test_broken_checks_column_gives_no_checks(self):
        self.assertEqual(build(sub(checks="kein json")).checks, [])

    def test_stored_clip_marker_is_translated(self):
        row = sub(
            log="a\n" + report.CLIP_MARK, error_log="x\n" + report.CLIP_MARK
        )
        self.assertTrue(
            report.build(row, "DE").output.endswith("[... gekürzt ...]")
        )
        self.assertTrue(
            report.build(row, "EN").output.endswith("[... truncated ...]")
        )
        self.assertTrue(
            report.build(row, "DE").errors.endswith("[... gekürzt ...]")
        )

    def test_text_form_for_the_archive(self):
        r = build(
            sub(
                finished=0,
                checks=[[1, "a"], [0, "b"]],
                log="hallo",
                error_log="error: x",
            )
        )
        text = report.as_text(r, "DE")
        self.assertIn("[OK] a\n[FAIL] b", text)
        self.assertIn("Ausgabe Ihres Programms:\nhallo", text)
        self.assertIn("Fehlermeldung:\nerror: x", text)


if __name__ == "__main__":
    unittest.main()
