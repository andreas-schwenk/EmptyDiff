# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import smtplib
import unittest
from unittest import mock

from helpers import STUDENT, DbTestCase
from emptydiff.core import mail


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = (
            host,
            port,
            timeout,
            context,
        )
        self.calls = []
        self.message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.message = message


class MailTest(DbTestCase):
    def setUp(self):
        super().setUp()
        FakeSMTP.instances.clear()

    def test_simulate_keeps_the_mail_in_memory_without_sending_or_history(
        self,
    ):
        with mock.patch("smtplib.SMTP") as smtp:
            mail.send(self.cfg, STUDENT, "Betreff", "Text")
        smtp.assert_not_called()
        self.assertEqual(
            mail.pop_simulated(),
            [{"recipient": STUDENT, "subject": "Betreff", "body": "Text"}],
        )
        self.assertEqual(
            mail.pop_simulated(), []
        )  # shown once, then gone: no history
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertNotIn("outbox", tables)

    def test_simulated_queue_is_bounded(self):
        for i in range(50):
            mail.send(self.cfg, STUDENT, f"B{i}", "T")
        self.assertEqual(len(mail.pop_simulated()), 20)

    def test_simulate_refused_outside_dev(self):
        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            with self.assertRaises(mail.MailError):
                mail.send(self.cfg, STUDENT, "B", "T")

    def smtp_cfg(self, **kw):
        values = dict(
            backend="smtp",
            host="smtp.example.org",
            port=25,
            sender="emptydiff@example.org",
        )
        self.cfg["mail"].update({**values, **kw})

    def test_smtp_plain_relay_without_tls_or_login(self):
        self.smtp_cfg()
        with mock.patch("smtplib.SMTP", FakeSMTP):
            mail.send(self.cfg, STUDENT, "Betreff", "Text")
        smtp = FakeSMTP.instances[0]
        self.assertEqual(
            (smtp.host, smtp.port, smtp.timeout), ("smtp.example.org", 25, 15)
        )
        self.assertEqual(smtp.calls, [])
        self.assertEqual(smtp.message["To"], STUDENT)
        self.assertEqual(smtp.message["From"], "emptydiff@example.org")
        self.assertEqual(smtp.message["Subject"], "Betreff")
        self.assertEqual(smtp.message["Auto-Submitted"], "auto-generated")
        self.assertTrue(smtp.message["Date"])
        self.assertTrue(smtp.message["Message-ID"].endswith("@example.org>"))

    def test_smtp_starttls_and_login(self):
        self.smtp_cfg(
            security="starttls",
            port=587,
            user="u",
            password="p",
            timeout_seconds=5,
        )
        with mock.patch("smtplib.SMTP", FakeSMTP):
            mail.send(self.cfg, STUDENT, "B", "T")
        smtp = FakeSMTP.instances[0]
        self.assertEqual((smtp.port, smtp.timeout), (587, 5))
        self.assertEqual(smtp.calls, ["starttls", ("login", "u", "p")])

    def test_smtp_ssl_uses_smtp_ssl_with_verified_context(self):
        self.smtp_cfg(security="ssl", port=465)
        with (
            mock.patch("smtplib.SMTP_SSL", FakeSMTP),
            mock.patch("smtplib.SMTP") as plain,
        ):
            mail.send(self.cfg, STUDENT, "B", "T")
        plain.assert_not_called()
        smtp = FakeSMTP.instances[0]
        self.assertEqual(smtp.port, 465)
        self.assertTrue(
            smtp.context.check_hostname
        )  # certificate and host name are verified

    def test_missing_host_or_sender_is_reported_not_sent(self):
        self.cfg["mail"].update(backend="smtp", host="", sender="")
        with (
            mock.patch("smtplib.SMTP") as smtp,
            self.assertRaises(mail.MailError),
        ):
            mail.send(self.cfg, STUDENT, "B", "T")
        smtp.assert_not_called()

    def test_smtp_errors_become_mail_error(self):
        self.smtp_cfg()
        with mock.patch(
            "smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "nein")
        ):
            with self.assertRaises(mail.MailError):
                mail.send(self.cfg, STUDENT, "B", "T")
        with mock.patch("smtplib.SMTP", side_effect=ConnectionRefusedError()):
            with self.assertRaises(mail.MailError):
                mail.send(self.cfg, STUDENT, "B", "T")

    def test_mail_test_command_sends_through_the_configured_backend(self):
        import contextlib
        import io

        from emptydiff import __main__ as cli

        path = os.path.join(self.tmp, "c.toml")
        with open(path, "w") as f:
            f.write(f'data_dir = "{self.tmp}"\n[mail]\nbackend = "simulate"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", path, "mail-test", "pruefer@example.org"])
        self.assertIn(
            "Simulation, nothing sent. To: pruefer@example.org", out.getvalue()
        )

    def test_login_code_mail_contains_code_and_ttl(self):
        subject, body = mail.login_code_mail("ABCD1234", 15, "DE")
        self.assertIn("ABCD1234", body)
        self.assertIn("15 Minuten", body)


if __name__ == "__main__":
    unittest.main()
