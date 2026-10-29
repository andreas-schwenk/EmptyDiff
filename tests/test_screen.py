# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

from emptydiff.core import config, screen

WORDS = config.DEFAULTS["languages"]["octave"]["suspicious_words"]


class ScreenTest(unittest.TestCase):
    def test_harmless_solution_has_no_flags(self):
        code = b"function y = addone(x)\n  y = x + 1;\n  disp(y);\nend\n"
        self.assertEqual(screen.scan({"addone.m": code}, WORDS), [])

    def test_whole_words_only(self):
        self.assertEqual(
            screen.scan(
                {"a.m": b"exited = 1; my_exit = 2; exit2 = 3;\n"}, WORDS
            ),
            [],
        )
        self.assertEqual(screen.scan({"a.m": b"exit(0)\n"}, WORDS), ["exit"])

    def test_words_with_special_characters(self):
        self.assertEqual(
            screen.scan({"a.m": b"disp('[OK] addone ok');\n"}, WORDS), ["[OK]"]
        )

    def test_order_follows_the_word_list_and_duplicates_collapse(self):
        code = b"fopen('x'); exit; exit; evalin('base', 'x');\n"
        self.assertEqual(
            screen.scan({"a.m": code}, WORDS), ["exit", "evalin", "fopen"]
        )

    def test_all_files_are_scanned_but_binary_files_are_not(self):
        files = {
            "a.m": b"x = 1;\n",
            "b.m": b"quit\n",
            "plot.png": b"\x89PNG\x00exit",
        }
        self.assertEqual(screen.scan(files, WORDS), ["quit"])

    def test_invalid_utf8_does_not_fail(self):
        self.assertEqual(
            screen.scan({"a.m": b"\xff\xfe exit\n"}, WORDS), ["exit"]
        )

    def test_custom_word_list(self):
        self.assertEqual(
            screen.scan({"a.m": b"pause(1)\n"}, ["pause"]), ["pause"]
        )


if __name__ == "__main__":
    unittest.main()
