# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

from helpers import GAUSS_FUNCTION
from emptydiff.core import provided

LANG = "octave"


class SplitTest(unittest.TestCase):
    def test_empty_text_has_no_functions(self):
        for text in ("", "  \n\n", None):
            self.assertEqual(provided.files(LANG, text), {})

    def test_every_function_becomes_a_file_named_after_it(self):
        text = (
            GAUSS_FUNCTION + "\nfunction twice_it(x)\n  disp(2 * "
            "x);\nendfunction\n\nfunction [a, b] = pair\n  a = "
            "1; b = 2;\nend\n"
        )
        result = provided.files(LANG, text)
        self.assertEqual(sorted(result), ["gauss.m", "pair.m", "twice_it.m"])
        self.assertEqual(result["gauss.m"], GAUSS_FUNCTION.encode())
        self.assertTrue(
            result["twice_it.m"].startswith(b"function twice_it(x)")
        )
        self.assertTrue(result["twice_it.m"].endswith(b"endfunction\n"))

    def test_all_header_forms(self):
        for header, name in (
            ("function y = f(x)", "f"),
            ("function [y, z] = g (x)", "g"),
            ("function h(x)", "h"),
            ("function k", "k"),
            ("  function  [ y ]=m(x) % comment", "m"),
            ("function n;", "n"),
        ):
            with self.subTest(header=header):
                self.assertEqual(
                    provided.names(LANG, header + "\nend\n"), [f"{name}.m"]
                )

    def test_comments_before_the_first_function_stay_with_it(self):
        result = provided.files(
            LANG, "% Solution of task 1\n# more\n" + GAUSS_FUNCTION
        )
        self.assertTrue(result["gauss.m"].startswith(b"% Solution of task 1"))

    def test_line_endings_are_normalized(self):
        self.assertEqual(
            provided.files(LANG, GAUSS_FUNCTION.replace("\n", "\r\n"))[
                "gauss.m"
            ],
            GAUSS_FUNCTION.encode(),
        )

    def test_errors(self):
        for text, key in (
            ("x = 1;", "ln.octave.err_provided_no_function"),
            ("x = 1;\n" + GAUSS_FUNCTION, "ln.octave.err_provided_outside"),
            (
                GAUSS_FUNCTION + GAUSS_FUNCTION,
                "ln.octave.err_provided_duplicate",
            ),
            ("function testbench()\nend", "ln.octave.err_provided_name"),
            ("function emptydiff_run()\nend", "ln.octave.err_provided_name"),
            ("function = 1", "ln.octave.err_provided_name"),
            (
                "function " + "a" * 70 + "()\nend",
                "ln.octave.err_provided_name",
            ),
        ):
            with (
                self.subTest(text=text[:25]),
                self.assertRaises(provided.ProvidedError) as ctx,
            ):
                provided.split(LANG, text)
            self.assertEqual(ctx.exception.key, key)

    def test_names_never_fails(self):
        self.assertEqual(provided.names(LANG, "kaputt"), [])
        self.assertEqual(
            provided.names("nonsense", GAUSS_FUNCTION), []
        )  # an unknown language shows nothing
        self.assertEqual(provided.names(LANG, GAUSS_FUNCTION), ["gauss.m"])


if __name__ == "__main__":
    unittest.main()
