# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from datetime import datetime, timedelta, timezone

from helpers import OTHER, STUDENT, DbTestCase
from emptydiff.core import auth, config

DOMAINS = [
    "student.example.edu",
    "example.edu",
    "student.example.org",
    "example.org",
]
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class NormalizeTest(unittest.TestCase):
    def test_accepts_allowed_domains_and_lowercases(self):
        for email in (
            "Max.Muster@Student.Example.EDU",
            " a@example.edu ",
            "a@student.example.org",
            "a@example.org",
        ):
            with self.subTest(email=email):
                self.assertEqual(
                    auth.normalize_email(email, DOMAINS), email.strip().lower()
                )

    def test_configured_domains_in_capitals_still_match(self):
        self.assertEqual(
            auth.normalize_email("a@example.edu", ["Example.EDU"]),
            "a@example.edu",
        )

    def test_rejects_other_or_tricky_domains(self):
        for email in (
            "a@gmail.com",
            "a@example.edu.evil.com",
            "a@evilexample.edu",
            "a@evil.com/example.edu",
            "a@student.example.edu@evil.com",
            "a b@example.edu",
            "@example.edu",
            "a@",
            "",
            None,
            "a@x.example.edu",
            "<a@example.edu>",
            "a\n@example.edu",
        ):
            with self.subTest(email=email):
                with self.assertRaises(auth.AuthError):
                    auth.normalize_email(email, DOMAINS)

    def test_code_normalization_is_crockford_tolerant(self):
        self.assertEqual(auth.normalize_code(" abcd-1o1l "), "ABCD1011")


class RememberTokenTest(DbTestCase):
    def count(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM remember_token"
        ).fetchone()[0]

    def test_issue_check_and_only_hash_is_stored(self):
        token = auth.issue_remember_token(self.conn, STUDENT, 180, now=T0)
        self.assertEqual(
            auth.check_remember_token(
                self.conn, token, 180, now=T0 + timedelta(days=1)
            ),
            STUDENT,
        )
        stored = self.conn.execute(
            "SELECT token_hash FROM remember_token"
        ).fetchone()[0]
        self.assertNotIn(token, stored)
        self.assertGreaterEqual(len(token), 40)

    def test_unknown_empty_or_oversized_tokens(self):
        auth.issue_remember_token(self.conn, STUDENT, 180, now=T0)
        for bad in (None, "", "x", "x" * 500):
            self.assertIsNone(
                auth.check_remember_token(self.conn, bad, 180, now=T0)
            )

    def test_expiry_and_sliding_renewal(self):
        token = auth.issue_remember_token(self.conn, STUDENT, 10, now=T0)
        self.assertEqual(
            auth.check_remember_token(
                self.conn, token, 10, now=T0 + timedelta(days=9)
            ),
            STUDENT,
        )
        # use on day 9 extended the validity to day 19
        self.assertEqual(
            auth.check_remember_token(
                self.conn, token, 10, now=T0 + timedelta(days=18)
            ),
            STUDENT,
        )
        self.assertIsNone(
            auth.check_remember_token(
                self.conn, token, 10, now=T0 + timedelta(days=29)
            )
        )

    def test_no_write_when_used_twice_within_a_day(self):
        token = auth.issue_remember_token(self.conn, STUDENT, 10, now=T0)
        before = self.conn.execute(
            "SELECT expires_at FROM remember_token"
        ).fetchone()[0]
        auth.check_remember_token(
            self.conn, token, 10, now=T0 + timedelta(hours=2)
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT expires_at FROM remember_token"
            ).fetchone()[0],
            before,
        )

    def test_revoke_only_affects_that_token(self):
        a = auth.issue_remember_token(self.conn, STUDENT, 180, now=T0)
        b = auth.issue_remember_token(
            self.conn, STUDENT, 180, now=T0
        )  # second device
        auth.revoke_remember_token(self.conn, a)
        self.assertIsNone(auth.check_remember_token(self.conn, a, 180, now=T0))
        self.assertEqual(
            auth.check_remember_token(self.conn, b, 180, now=T0), STUDENT
        )
        auth.revoke_remember_token(self.conn, None)  # no error

    def test_expired_tokens_are_purged_on_issue(self):
        auth.issue_remember_token(self.conn, OTHER, 1, now=T0)
        auth.issue_remember_token(
            self.conn, STUDENT, 1, now=T0 + timedelta(days=2)
        )
        self.assertEqual(self.count(), 1)


class CodeTest(DbTestCase):
    def test_issue_and_verify_once(self):
        code = auth.issue_code(self.conn, STUDENT, "1.2.3.4", now=T0)
        self.assertEqual(len(code), auth.CODE_LENGTH)
        self.assertTrue(set(code) <= set(auth.ALPHABET))
        self.assertTrue(
            auth.verify_code(
                self.conn, STUDENT, code, now=T0 + timedelta(minutes=1)
            )
        )
        self.assertFalse(
            auth.verify_code(
                self.conn, STUDENT, code, now=T0 + timedelta(minutes=2)
            )
        )  # single use

    def test_code_is_stored_only_as_hash(self):
        code = auth.issue_code(self.conn, STUDENT, None, now=T0)
        stored = self.conn.execute(
            "SELECT code_hash FROM login_code"
        ).fetchone()[0]
        self.assertNotIn(code, stored)
        self.assertEqual(len(stored), 64)

    def test_input_tolerance(self):
        code = auth.issue_code(self.conn, STUDENT, None, now=T0)
        self.assertTrue(
            auth.verify_code(
                self.conn, STUDENT, f" {code[:4].lower()}-{code[4:]} ", now=T0
            )
        )

    def test_expiry(self):
        code = auth.issue_code(
            self.conn, STUDENT, None, ttl_minutes=15, now=T0
        )
        self.assertFalse(
            auth.verify_code(
                self.conn,
                STUDENT,
                code,
                now=T0 + timedelta(minutes=15, seconds=1),
            )
        )

    def test_wrong_code_and_lockout(self):
        code = auth.issue_code(self.conn, STUDENT, None, now=T0)
        for _ in range(auth.MAX_ATTEMPTS):
            self.assertFalse(
                auth.verify_code(self.conn, STUDENT, "WRONG123", now=T0)
            )
        # after too many failed attempts even the right code is locked
        self.assertFalse(auth.verify_code(self.conn, STUDENT, code, now=T0))

    def test_code_belongs_to_address(self):
        code = auth.issue_code(self.conn, STUDENT, None, now=T0)
        self.assertFalse(auth.verify_code(self.conn, OTHER, code, now=T0))

    def test_new_code_invalidates_old_one(self):
        old = auth.issue_code(self.conn, STUDENT, None, now=T0)
        new = auth.issue_code(
            self.conn, STUDENT, None, now=T0 + timedelta(minutes=1)
        )
        self.assertFalse(
            auth.verify_code(
                self.conn, STUDENT, old, now=T0 + timedelta(minutes=2)
            )
        )
        self.assertTrue(
            auth.verify_code(
                self.conn, STUDENT, new, now=T0 + timedelta(minutes=2)
            )
        )

    def test_no_code_issued(self):
        self.assertFalse(
            auth.verify_code(self.conn, STUDENT, "ABCD1234", now=T0)
        )

    def test_rate_limit_per_address(self):
        for i in range(auth.MAX_CODES_PER_EMAIL_HOUR):
            auth.issue_code(
                self.conn, STUDENT, None, now=T0 + timedelta(minutes=i)
            )
        with self.assertRaises(auth.AuthError):
            auth.issue_code(
                self.conn, STUDENT, None, now=T0 + timedelta(minutes=10)
            )
        # possible again after an hour, other addresses are not affected
        auth.issue_code(
            self.conn, STUDENT, None, now=T0 + timedelta(hours=1, minutes=10)
        )
        auth.issue_code(self.conn, OTHER, None, now=T0 + timedelta(minutes=10))

    def test_rate_limit_per_ip(self):
        for i in range(auth.MAX_CODES_PER_IP_HOUR):
            auth.issue_code(
                self.conn, f"user{i}@example.edu", "9.9.9.9", now=T0
            )
        with self.assertRaises(auth.AuthError):
            auth.issue_code(self.conn, "neu@example.edu", "9.9.9.9", now=T0)
        auth.issue_code(self.conn, "neu@example.edu", "8.8.8.8", now=T0)

    def test_limits_can_be_set(self):
        for i in range(3):
            auth.issue_code(
                self.conn, STUDENT, "7.7.7.7", now=T0, per_email=3, per_ip=100
            )
        with self.assertRaises(auth.AuthError) as ctx:
            auth.issue_code(
                self.conn, STUDENT, "7.7.7.7", now=T0, per_email=3, per_ip=100
            )
        self.assertEqual(ctx.exception.key, "err.auth.too_many_email")
        with self.assertRaises(auth.AuthError) as ctx:
            auth.issue_code(
                self.conn, OTHER, "7.7.7.7", now=T0, per_email=3, per_ip=3
            )
        self.assertEqual(ctx.exception.key, "err.auth.too_many_ip")

    def test_the_default_limits_allow_a_computer_room_behind_one_address(self):
        self.assertGreaterEqual(auth.MAX_CODES_PER_IP_HOUR, 100)

    def test_cooldown_between_requests_of_one_address(self):
        auth.issue_code(self.conn, STUDENT, None, now=T0, cooldown_seconds=30)
        with self.assertRaises(auth.AuthError) as ctx:
            auth.issue_code(
                self.conn,
                STUDENT,
                None,
                now=T0 + timedelta(seconds=10),
                cooldown_seconds=30,
            )
        self.assertEqual(
            (ctx.exception.key, ctx.exception.params),
            ("err.auth.cooldown", {"seconds": 20}),
        )
        auth.issue_code(
            self.conn,
            OTHER,
            None,
            now=T0 + timedelta(seconds=10),
            cooldown_seconds=30,
        )  # other address
        auth.issue_code(
            self.conn,
            STUDENT,
            None,
            now=T0 + timedelta(seconds=31),
            cooldown_seconds=30,
        )

    def test_a_refused_request_is_not_counted(self):
        auth.issue_code(
            self.conn, STUDENT, None, now=T0, cooldown_seconds=30, per_email=2
        )
        # refused by the cooldown: leaves no row, so nobody can be
        # locked out this way
        for _ in range(5):
            with self.assertRaises(auth.AuthError):
                auth.issue_code(
                    self.conn,
                    STUDENT,
                    None,
                    now=T0 + timedelta(seconds=5),
                    cooldown_seconds=30,
                    per_email=2,
                )
        auth.issue_code(
            self.conn,
            STUDENT,
            None,
            now=T0 + timedelta(seconds=40),
            cooldown_seconds=30,
            per_email=2,
        )


if __name__ == "__main__":
    unittest.main()
