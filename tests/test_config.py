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

    def test_proxy_addresses_are_validated(self):
        base = 'base_url = "https://host.school.invalid/lab"\n'
        cfg = config.load(
            _write(
                base + '[proxy]\naddresses = ["192.0.2.20", " 10.0.0.0/8 "]'
            )
        )
        self.assertEqual(
            cfg["proxy"]["addresses"], ["192.0.2.20", "10.0.0.0/8"]
        )
        self.assertEqual(config.load(_write(base))["proxy"]["addresses"], [])
        for bad in (
            '["host.example"]',
            '"192.0.2.20"',
            "[1]",
            '["300.1.1.1"]',
        ):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.load(_write(base + f"[proxy]\naddresses = {bad}"))
        with self.assertRaises(
            config.ConfigError
        ):  # the public address is https
            config.load(
                _write(
                    'base_url = "http://host.school.invalid"\n'
                    '[proxy]\naddresses = ["192.0.2.20"]'
                )
            )

    def test_base_url_path_is_the_url_prefix(self):
        for url, prefix in (
            ("https://h.school.invalid", ""),
            ("https://h.school.invalid/", ""),
            ("https://h.school.invalid/lab", "/lab"),
            ("https://h.school.invalid/lab/", "/lab"),
            ("https://h.school.invalid/a/b-c_d", "/a/b-c_d"),
        ):
            with self.subTest(url=url):
                cfg = config.load(_write(f'base_url = "{url}"'))
                self.assertEqual(config.url_prefix(cfg), prefix)
        for bad in ("/Lab", "/a b", "/a//b", "/..", "/-x"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.load(
                    _write(f'base_url = "https://h.school.invalid{bad}"')
                )

    def test_client_address_is_taken_from_the_header_only_behind_the_proxy(
        self,
    ):
        cfg = config.load(
            _write(
                'base_url = "https://h.school.invalid/lab"\n'
                '[proxy]\naddresses = ["192.0.2.20", "198.51.100.0/24"]'
            )
        )

        def ca(remote, header=""):
            return config.client_address(cfg, remote, header)

        self.assertEqual(ca("192.0.2.20", "203.0.113.9"), "203.0.113.9")
        # a client that sends its own header: the proxy appends the
        # real address, which stands last
        self.assertEqual(
            ca("192.0.2.20", "1.1.1.1, 203.0.113.9"), "203.0.113.9"
        )
        self.assertEqual(
            ca("192.0.2.20", "203.0.113.9, 198.51.100.7"), "203.0.113.9"
        )  # a second proxy in the chain is skipped
        self.assertEqual(ca("192.0.2.20", ""), "192.0.2.20")
        self.assertEqual(ca("192.0.2.20", "not an address"), "192.0.2.20")
        # not from the proxy: the header is just a claim
        self.assertEqual(ca("203.0.113.50", "1.1.1.1"), "203.0.113.50")
        self.assertEqual(ca(None, "1.1.1.1"), None)
        plain = config.load(_write('base_url = "https://h.school.invalid"'))
        self.assertEqual(
            config.client_address(plain, "192.0.2.20", "1.1.1.1"), "192.0.2.20"
        )

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
