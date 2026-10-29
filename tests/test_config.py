# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import unittest
from unittest import mock

from emptydiff.core import config


def _write(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return f.name


class ConfigTest(unittest.TestCase):
    def test_attempt_limit_is_one_static_number_from_the_config(self):
        self.assertEqual(config.DEFAULTS["limits"]["max_attempts"], 7)
        for bad in ("0", "101", "'7'", "true", "2.5"):
            path = _write(f"[limits]\nmax_attempts = {bad}\n")
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.load(path)
        self.assertEqual(
            config.load(_write("[limits]\nmax_attempts = 4\n"))["limits"][
                "max_attempts"
            ],
            4,
        )

    def test_grading_is_sequential_with_a_batch_size(self):
        self.assertEqual(config.DEFAULTS["grader"]["batch_size"], 10)
        self.assertNotIn("workers", config.DEFAULTS["grader"])
        for bad in ("0", "1001", "'5'", "true", "2.5"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.load(_write(f"[grader]\nbatch_size = {bad}\n"))
        self.assertEqual(
            config.load(_write("[grader]\nbatch_size = 3\n"))["grader"][
                "batch_size"
            ],
            3,
        )

    def test_suspicious_words_must_be_a_list_of_strings(self):
        self.assertIn(
            "exit", config.DEFAULTS["languages"]["octave"]["suspicious_words"]
        )
        for bad in ("'exit'", "[1]", "['']"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.load(
                    _write(f"[languages.octave]\nsuspicious_words = {bad}\n")
                )
        self.assertEqual(
            config.load(
                _write("[languages.octave]\nsuspicious_words = ['pause']\n")
            )["languages"]["octave"]["suspicious_words"],
            ["pause"],
        )

    def test_uses_https_follows_the_scheme_of_the_base_url(self):
        for url, expected in (
            ("https://emptydiff.school.invalid", True),
            ("HTTPS://emptydiff.school.invalid", True),
            ("http://labserver", False),
            ("http://127.0.0.1:8091", False),
        ):
            with self.subTest(url=url):
                self.assertIs(config.uses_https({"base_url": url}), expected)

    def test_login_limits_have_defaults_and_are_validated(self):
        auth = config.DEFAULTS["auth"]
        self.assertEqual(
            (
                auth["max_codes_per_email_hour"],
                auth["max_codes_per_ip_hour"],
                auth["code_cooldown_seconds"],
                auth["min_form_seconds"],
            ),
            (8, 200, 30, 2),
        )
        for key, bad in (
            ("max_codes_per_email_hour", 0),
            ("max_codes_per_ip_hour", "'x'"),
            ("code_cooldown_seconds", -1),
            ("min_form_seconds", 3.5),
            ("min_form_seconds", "true"),
        ):
            with (
                self.subTest(key=key, bad=bad),
                self.assertRaises(config.ConfigError),
            ):
                config.load(_write(f"[auth]\n{key} = {bad}\n"))
        self.assertEqual(
            config.load(_write("[auth]\ncode_cooldown_seconds = 0\n"))["auth"][
                "code_cooldown_seconds"
            ],
            0,
        )

    def test_addresses_and_domains_in_the_config_are_lowercased(self):
        cfg = config.load(
            _write(
                '[auth]\nallowed_domains = [" Student.Example.EDU '
                '"]\nadmins = ["Dozent@Example.EDU"]\n'
            )
        )
        self.assertEqual(
            cfg["auth"]["allowed_domains"], ["student.example.edu"]
        )
        self.assertEqual(cfg["auth"]["admins"], ["dozent@example.edu"])

    def test_defaults_merged(self):
        path = _write('[auth]\nadmins = ["a@example.edu"]\n')
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            cfg = config.load(path)
        self.assertEqual(cfg["auth"]["admins"], ["a@example.edu"])
        self.assertEqual(cfg["mail"]["backend"], "smtp")

    def test_simulate_requires_dev(self):
        path = _write('[mail]\nbackend = "simulate"\n')
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            with self.assertRaises(config.ConfigError):
                config.load(path)
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": "1"}):
            self.assertEqual(config.load(path)["mail"]["backend"], "simulate")

    def test_no_sandbox_requires_dev(self):
        path = _write('[grader]\nsandbox = "none"\n')
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            with self.assertRaises(config.ConfigError):
                config.load(path)

    def test_missing_file(self):
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            with self.assertRaises(config.ConfigError):
                config.load("/nonexistent/config.toml")


if __name__ == "__main__":
    unittest.main()
