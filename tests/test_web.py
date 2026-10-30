# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import io
import json
import os
import re
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest import mock

try:
    import flask  # noqa: F401
except ImportError:  # e.g. natively on macOS without Flask
    flask = None

from helpers import (
    ADMIN,
    GAUSS_FUNCTION,
    GOOD,
    MODES,
    OTHER,
    PAST,
    STUDENT,
    DbTestCase,
    add_lab,
)
from test_languages import with_toy
from emptydiff.core import auth, db, labs, mail, submissions, worker

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
CODE_RE = re.compile(r"^    ([0-9A-Z]{8})$", re.M)


@unittest.skipIf(
    flask is None,
    "Flask nicht installiert (Tests im dev/-Container ausführen)",
)
class WebTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        from emptydiff.web import create_app

        self.app = create_app(self.cfg)
        self.client = self.app.test_client()

    # --- Helpers ---
    def token(self, client, path="/login"):
        return CSRF_RE.search(client.get(path).get_data(as_text=True)).group(1)

    def login(self, email=STUDENT, client=None, remember=False):
        client = client or self.client
        data = {"email": email, "csrf_token": self.token(client)}
        if remember:
            data["remember"] = "1"
        client.post("/login", data=data)
        body = next(
            m["body"]
            for m in reversed(mail.SIMULATED)
            if m["recipient"] == email
        )
        code = CODE_RE.search(body).group(1)
        resp = client.post(
            "/login/verify",
            data={
                "code": code,
                "csrf_token": self.token(client, "/login/verify"),
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.last_login_response = resp
        return client

    def upload(self, client, files, lab="am24-1a"):
        data = {
            "csrf_token": self.token(client, f"/lab/{lab}"),
            "files": [
                (io.BytesIO(content), name) for name, content in files.items()
            ],
        }
        return client.post(
            f"/lab/{lab}", data=data, content_type="multipart/form-data"
        )


class LoginProtectionTest(WebTestCase):
    def codes(self):
        return self.conn.execute("SELECT COUNT(*) FROM login_code").fetchone()[
            0
        ]

    def request_code(self, extra=None, email=STUDENT, client=None):
        client = client or self.client
        data = {
            "email": email,
            "csrf_token": self.token(client),
            **(extra or {}),
        }
        return client.post("/login", data=data)

    def test_login_page_states_the_purpose_at_the_top(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertLess(
            page.index("EmptyDiff prüft Ihre Programme"),
            page.index("<h1>Anmelden</h1>"),
        )

    def test_login_form_has_a_hidden_field_that_people_do_not_reach(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertRegex(
            page,
            r'(?s)<div class="hp" '
            r'aria-hidden="true">.*name="extra_url" '
            r'tabindex="-1" autocomplete="off"',
        )

    def test_filled_hidden_field_sends_no_mail_but_looks_like_success(self):
        resp = self.request_code({"extra_url": "http://spam.example"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.codes(), 0)

    def test_form_sent_too_fast_is_refused_with_a_hint_and_works_later(self):
        self.cfg["auth"]["min_form_seconds"] = 2
        self.client.get("/login")
        resp = self.client.post(
            "/login",
            data={
                "email": STUDENT,
                "csrf_token": self.token(self.client, "/login"),
            },
            follow_redirects=True,
        )
        self.assertIn("zu schnell", resp.get_data(as_text=True))
        self.assertEqual(self.codes(), 0)
        token = self.token(
            self.client, "/login"
        )  # showing the form again starts the clock
        with mock.patch(
            "emptydiff.web.time.time", return_value=time.time() + 5
        ):
            self.client.post(
                "/login", data={"email": STUDENT, "csrf_token": token}
            )
        self.assertEqual(self.codes(), 1)

    def test_a_post_without_having_seen_the_form_is_refused(self):
        self.cfg["auth"]["min_form_seconds"] = 2
        with self.client.session_transaction() as s:
            s["csrf"] = "x"
        resp = self.client.post(
            "/login", data={"email": STUDENT, "csrf_token": "x"}
        )
        self.assertEqual(self.codes(), 0)
        self.assertEqual(resp.status_code, 302)

    def test_cooldown_message_shows_the_seconds(self):
        self.cfg["auth"]["code_cooldown_seconds"] = 30
        self.request_code()
        page = self.client.post(
            "/login",
            data={"email": STUDENT, "csrf_token": self.token(self.client)},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertRegex(page, r"Bitte warten Sie noch \d+ Sekunden")
        self.assertEqual(self.codes(), 1)

    def test_limits_come_from_the_configuration(self):
        self.cfg["auth"]["max_codes_per_email_hour"] = 2
        for _ in range(2):
            self.request_code()
        page = self.client.post(
            "/login",
            data={"email": STUDENT, "csrf_token": self.token(self.client)},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("zu viele Codes", page)
        self.assertEqual(self.codes(), 2)


class LoginTest(WebTestCase):
    def test_unauthenticated_requests_redirect_to_login(self):
        add_lab(self.conn)
        for path in ("/", "/lab/am24-1a", "/submission/1"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.headers["Location"].endswith("/login"))

    def test_login_flow_shows_simulated_mail_as_modal(self):
        add_lab(self.conn)
        resp = self.client.post(
            "/login",
            data={"email": STUDENT, "csrf_token": self.token(self.client)},
            follow_redirects=True,
        )
        page = resp.get_data(as_text=True)
        self.assertIn("<dialog data-autoshow open>", page)
        self.assertIn("Simulierte Mail", page)
        self.assertIn(STUDENT, page)
        code = CODE_RE.search(page).group(1)
        # the modal appears only once
        self.assertNotIn(
            "<dialog", self.client.get("/login/verify").get_data(as_text=True)
        )
        resp = self.client.post(
            "/login/verify",
            data={
                "code": code,
                "csrf_token": self.token(self.client, "/login/verify"),
            },
            follow_redirects=True,
        )
        self.assertIn("Praktikumsversuche", resp.get_data(as_text=True))
        self.assertIn(STUDENT, resp.get_data(as_text=True))

    def test_foreign_domain_is_rejected_without_mail(self):
        resp = self.client.post(
            "/login",
            data={
                "email": "x@gmail.com",
                "csrf_token": self.token(self.client),
            },
            follow_redirects=True,
        )
        self.assertIn(
            "Mailadresse Ihrer Einrichtung", resp.get_data(as_text=True)
        )
        self.assertEqual(len(mail.SIMULATED), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM login_code").fetchone()[0],
            0,
        )

    def test_wrong_code_and_lockout(self):
        self.client.post(
            "/login",
            data={"email": STUDENT, "csrf_token": self.token(self.client)},
        )
        code = CODE_RE.search(mail.SIMULATED[-1]["body"]).group(
            1
        )  # before the first page view (shows and empties the modal)
        for _ in range(auth.MAX_ATTEMPTS):
            resp = self.client.post(
                "/login/verify",
                follow_redirects=True,
                data={
                    "code": "WRONG123",
                    "csrf_token": self.token(self.client, "/login/verify"),
                },
            )
            self.assertIn(
                "ungültig oder abgelaufen", resp.get_data(as_text=True)
            )
        resp = self.client.post(
            "/login/verify",
            data={
                "code": code,
                "csrf_token": self.token(self.client, "/login/verify"),
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            resp.headers["Location"].endswith("/login/verify")
        )  # locked, not logged in
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_post_without_csrf_token_is_rejected(self):
        self.assertEqual(
            self.client.post("/login", data={"email": STUDENT}).status_code,
            400,
        )
        self.assertEqual(self.client.post("/logout").status_code, 400)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM login_code").fetchone()[0],
            0,
        )

    def test_logout(self):
        self.login()
        self.assertEqual(self.client.get("/").status_code, 200)
        token = self.token(self.client, "/")
        self.assertEqual(
            self.client.post(
                "/logout", data={"csrf_token": token}
            ).status_code,
            302,
        )
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_a_mail_failure_still_opens_the_code_page(self):
        with mock.patch.object(mail, "send", side_effect=mail.MailError("x")):
            resp = self.client.post(
                "/login",
                data={"email": STUDENT, "csrf_token": self.token(self.client)},
            )
        self.assertTrue(resp.headers["Location"].endswith("/login/verify"))
        page = self.client.get("/login/verify").get_data(as_text=True)
        self.assertIn("konnte nicht versendet werden", page)
        self.assertIn("Betreuer einen Code nennen", page)
        code = auth.issue_code(
            self.conn, STUDENT, None, self.cfg["auth"]["code_ttl_minutes"]
        )  # what `python3 -m emptydiff issue-code` prints
        resp = self.client.post(
            "/login/verify",
            data={
                "code": code,
                "csrf_token": self.token(self.client, "/login/verify"),
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_login_page_asks_for_consent_to_the_cookie(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('name="remember"', page)
        self.assertNotIn(
            'name="remember" value="1" checked', page
        )  # not preselected
        self.assertIn("Ich stimme zu", page)
        self.assertIn("180 Tage", page)
        self.assertIn("technisch notwendige Cookies", page)

    def test_security_headers(self):
        headers = self.client.get("/login").headers
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        csp = headers["Content-Security-Policy"]
        for directive in (
            "default-src 'self'",
            "base-uri 'none'",
            "form-action 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
        ):
            self.assertIn(directive, csp)
        self.assertEqual(headers["X-Robots-Tag"], "noindex, nofollow")

    def test_page_frame_has_skip_link_robots_meta_and_icons(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn(
            '<a class="skip" href="#main">Zum Inhalt springen</a>', page
        )
        self.assertIn('<main id="main"', page)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', page)
        self.assertIn(
            'rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=',
            page,
        )
        self.assertIn('href="/static/favicon.ico?v=', page)
        self.assertIn(
            'role="status" aria-live="polite"', page
        )  # for announcing results

    def test_icons_are_served_at_the_root_and_are_valid(self):
        ico = self.client.get("/favicon.ico")
        self.assertEqual(
            (ico.status_code, ico.mimetype), (200, "image/vnd.microsoft.icon")
        )
        self.assertEqual(
            ico.data[:4], b"\x00\x00\x01\x00"
        )  # ICO header, image type 1
        touch = self.client.get("/apple-touch-icon.png")
        self.assertEqual(
            (touch.status_code, touch.mimetype), (200, "image/png")
        )
        self.assertEqual(touch.data[:8], b"\x89PNG\r\n\x1a\n")
        svg = self.client.get("/static/favicon.svg")
        self.assertEqual(svg.status_code, 200)
        self.assertIn(b"<circle", svg.data)  # the empty-set sign

    def test_table_heads_have_a_scope(self):
        import glob
        import re

        for path in glob.glob(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "emptydiff",
                "templates",
                "*.html",
            )
        ):
            with self.subTest(template=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(
                        re.findall(r"<th(?![^>]*scope=)[ >]", f.read()), []
                    )

    def test_there_is_no_mail_history_page_or_table(self):
        self.assertEqual(self.client.get("/dev/outbox").status_code, 404)
        self.assertNotIn(
            "Simulierte Mails", self.login().get("/").get_data(as_text=True)
        )
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertNotIn("outbox", tables)

    def test_simulated_mails_are_not_shown_in_production(self):
        from emptydiff.web import create_app

        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            cfg = dict(self.cfg, data_dir=os.path.join(self.tmp, "prod"))
            cfg["mail"] = dict(
                self.cfg["mail"],
                backend="smtp",
                host="smtp.example.org",
                sender="m@example.org",
            )
            client = create_app(cfg).test_client()
            mail.SIMULATED.append(
                {
                    "recipient": "x@example.org",
                    "subject": "geheim",
                    "body": "CODE",
                }
            )
            self.assertNotIn(
                "<dialog", client.get("/login").get_data(as_text=True)
            )

    def test_production_start_requires_mandatory_settings(self):
        from emptydiff.core.config import ConfigError
        from emptydiff.web import create_app

        with mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""}):
            cfg = dict(self.cfg, data_dir=os.path.join(self.tmp, "prod2"))
            cfg["auth"] = dict(self.cfg["auth"], allowed_domains=[], admins=[])
            cfg["mail"] = dict(
                self.cfg["mail"], backend="smtp", host="", sender=""
            )
            with self.assertRaises(ConfigError) as ctx:
                create_app(cfg)
        for key in (
            "auth.allowed_domains",
            "auth.admins",
            "mail.host",
            "mail.sender",
        ):
            self.assertIn(key, str(ctx.exception))


class SubmissionFlowTest(WebTestCase):
    def test_index_lists_active_labs_only(self):
        add_lab(self.conn, name="am24-1a")
        add_lab(self.conn, name="am20-1a", active=0)
        page = self.login().get("/").get_data(as_text=True)
        self.assertIn("am24-1a", page)
        self.assertNotIn("am20-1a", page)

    def test_inactive_or_unknown_lab_is_404(self):
        add_lab(self.conn, name="am20-1a", active=0)
        client = self.login()
        self.assertEqual(client.get("/lab/am20-1a").status_code, 404)
        self.assertEqual(client.get("/lab/gibtsnicht").status_code, 404)

    def test_invalid_upload_shows_message(self):
        add_lab(self.conn)
        client = self.login()
        resp = self.upload(client, {"x y.m": b"1"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(
            "Ungültige Dateinamen",
            client.get("/lab/am24-1a").get_data(as_text=True),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
            0,
        )

    def test_upload_without_file(self):
        add_lab(self.conn)
        client = self.login()
        client.post(
            "/lab/am24-1a",
            data={"csrf_token": self.token(client, "/lab/am24-1a")},
        )
        self.assertIn(
            "mindestens eine Datei",
            client.get("/lab/am24-1a").get_data(as_text=True),
        )

    def test_path_in_client_filename_is_reduced_to_basename(self):
        add_lab(self.conn)
        client = self.login()
        resp = self.upload(client, {"../../etc/addone.m": GOOD})
        self.assertEqual(resp.status_code, 302)
        sub = self.conn.execute("SELECT * FROM submission").fetchone()
        from emptydiff.core import submissions

        self.assertEqual(
            list(submissions.load_files(self.cfg, sub)), ["addone.m"]
        )

    def test_pending_page_refreshes_and_other_students_cannot_see_it(self):
        add_lab(self.conn)
        client = self.login()
        location = self.upload(client, {"addone.m": GOOD}).headers["Location"]
        page = client.get(location).get_data(as_text=True)
        self.assertIn(
            '<noscript><meta http-equiv="refresh"', page
        )  # only without JavaScript; otherwise app.js asks again
        self.assertIn("data-poll", page)
        self.assertIn("wird geprüft", page)
        other = self.login(OTHER, self.app.test_client())
        self.assertEqual(other.get(location).status_code, 404)
        admin = self.login(ADMIN, self.app.test_client())
        self.assertEqual(admin.get(location).status_code, 200)
        self.assertIn(STUDENT, admin.get(location).get_data(as_text=True))

    def test_second_upload_while_pending_is_refused(self):
        add_lab(self.conn)
        client = self.login()
        self.upload(client, {"addone.m": GOOD})
        self.upload(client, {"addone.m": GOOD})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
            1,
        )
        self.assertIn(
            "noch geprüft", client.get("/lab/am24-1a").get_data(as_text=True)
        )


@unittest.skipUnless(
    MODES, "octave-cli nicht verfügbar (Tests im dev/-Container ausführen)"
)
class EndToEndTest(WebTestCase):
    def finish(self, client, location):
        self.assertTrue(worker.run_batch(self.conn, self.cfg))
        return client.get(location).get_data(as_text=True)

    def test_passing_submission_report(self):
        add_lab(self.conn)
        client = self.login()
        location = self.upload(client, {"addone.m": GOOD}).headers["Location"]
        page = self.finish(client, location)
        self.assertNotIn('http-equiv="refresh"', page)
        self.assertIn("Automatische Tests bestanden", page)
        self.assertNotIn("Betreuer können Abgaben", page)
        self.assertRegex(
            page,
            r'<li class="ok"><span class="mark" '
            r'aria-hidden="true">✓</span>.*addone ok',
        )
        self.assertIn("Abgegebene Dateien", page)  # the own source
        overview = client.get("/").get_data(as_text=True)
        self.assertIn("bestanden", overview)
        self.assertIn("✓ bestanden", overview)
        self.assertIn(
            "✓ bestanden", client.get("/lab/am24-1a").get_data(as_text=True)
        )  # state right at the lab
        self.assertEqual(
            client.get("/credit").status_code, 404
        )  # there is no separate credit page for students
        self.assertNotIn("Mein Testat", overview)

    def test_failing_submission_and_resubmission(self):
        add_lab(self.conn)
        client = self.login()
        wrong = b"function y = addone(x)\n  y = x + 2;\nend\n"
        page = self.finish(
            client,
            self.upload(client, {"addone.m": wrong}).headers["Location"],
        )
        self.assertIn("nicht bestanden", page)
        self.assertRegex(
            page,
            r'<li class="fail"><span class="mark" '
            r'aria-hidden="true">✗</span>.*addone falsch',
        )
        page = self.finish(
            client, self.upload(client, {"addone.m": GOOD}).headers["Location"]
        )
        self.assertIn("Abgabe 2 zu", page)
        self.assertIn("Automatische Tests bestanden", page)
        overview = client.get("/").get_data(as_text=True)
        self.assertIn(
            "Für das Testat zählt jeweils Ihre <b>letzte</b>", overview
        )
        self.assertIn(
            "✓ bestanden", overview
        )  # the last submission counts, not the first

    def test_unexpected_file_is_refused_at_upload(self):
        add_lab(self.conn)
        client = self.login()
        resp = self.upload(client, {"addone.m": GOOD, "disp.m": b"x = 1;\n"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/lab/am24-1a")
        page = client.get("/lab/am24-1a").get_data(as_text=True)
        self.assertIn("nicht erwartet: disp.m", page)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
            0,
        )

    def test_conspicuous_submission_is_flagged_for_admins_only(self):
        add_lab(self.conn)
        client = self.login()
        sneaky = (
            b"function y = addone(x)\n  y = x + 1;\n  if x > 100, "
            b"exit(0); end\nend\n"
        )
        location = self.upload(client, {"addone.m": sneaky}).headers[
            "Location"
        ]
        student_page = self.finish(client, location)
        self.assertNotIn("Auffällig", student_page)
        self.assertNotIn("auffällig", student_page)
        admin = self.login(ADMIN, self.app.test_client())
        self.assertIn(
            "Auffällig (nur für Admins sichtbar)",
            admin.get(location).get_data(as_text=True),
        )
        listing = admin.get("/admin/lab/am24-1a/submissions").get_data(
            as_text=True
        )
        self.assertIn("auffällig", listing)
        self.assertIn("Der Quelltext enthält auffällige Wörter: exit", listing)

    def test_report_text_is_escaped(self):
        add_lab(self.conn)
        evil = (
            b"function y = addone(x)\n  "
            b"disp('<script>alert(1)</script>');\n  y = x + 1;\nend\n"
        )
        client = self.login()
        page = self.finish(
            client, self.upload(client, {"addone.m": evil}).headers["Location"]
        )
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_late_submission_is_marked(self):
        add_lab(self.conn, due=PAST)
        client = self.login()
        page = self.finish(
            client, self.upload(client, {"addone.m": GOOD}).headers["Location"]
        )
        self.assertIn("verspätet", page)
        self.assertIn("Automatische Tests bestanden", page)  # tested anyway

    def test_incomplete_submission_names_missing_files(self):
        add_lab(self.conn, required="addone.m\nplot.png\n")
        client = self.login()
        page = self.finish(
            client, self.upload(client, {"addone.m": GOOD}).headers["Location"]
        )
        self.assertIn("Es fehlen: plot.png", page)
        overview = client.get("/").get_data(
            as_text=True
        )  # passed tests but incomplete: no credit
        self.assertIn("✗ unvollständig", overview)
        self.assertNotIn("✓ bestanden", overview)

    def test_back_link_follows_the_page_hierarchy(self):
        add_lab(self.conn)
        client = self.login()
        location = self.upload(client, {"addone.m": GOOD}).headers["Location"]
        self.assertIn(
            'href="/lab/am24-1a">← Versuch am24-1a',
            client.get(location).get_data(as_text=True),
        )
        admin = self.login(ADMIN, self.app.test_client())
        page = admin.get(location).get_data(
            as_text=True
        )  # admin sees other people's files: back to the submission list
        self.assertIn(
            'href="/admin/lab/am24-1a/submissions">← Abgaben am24-1a', page
        )
        self.assertNotIn("← Versuch", page)
        self.assertRegex(
            page, r'<a href="/admin/" aria-current="page">Verwaltung</a>'
        )
        self.assertNotRegex(page, r'<a href="/" aria-current="page">')

    def test_manual_lab_shows_only_receipt_to_students(self):
        add_lab(self.conn, show_report=0)
        client = self.login()
        location = self.upload(client, {"addone.m": GOOD}).headers["Location"]
        page = self.finish(client, location)
        self.assertIn("erfolgreich eingegangen", page)
        self.assertNotIn("bestanden", page)
        self.assertNotIn("addone ok", page)
        overview = client.get("/").get_data(as_text=True)
        self.assertNotIn(
            "✓ bestanden", overview
        )  # no result badge, only the receipt state
        self.assertNotIn("✗ nicht bestanden", overview)
        self.assertIn("eingegangen", overview)
        admin = self.login(ADMIN, self.app.test_client())
        self.assertIn("addone ok", admin.get(location).get_data(as_text=True))


class CspTest(unittest.TestCase):
    def test_templates_have_no_inline_styles(self):
        """The Content-Security-Policy ('default-src self') blocks style
        attributes: they must not be used."""
        import glob
        import re

        for path in glob.glob(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "emptydiff",
                "templates",
                "*.html",
            )
        ):
            with self.subTest(template=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    self.assertIsNone(re.search(r"<[^>]*\sstyle=", f.read()))


class RememberTest(WebTestCase):
    COOKIE = "emptydiff_remember"

    def test_under_a_path_links_and_cookies_stay_below_it(self):
        add_lab(self.conn)
        client = self.app.test_client()
        base = "http://localhost/lab"
        page = client.get("/login", base_url=base).get_data(as_text=True)
        links = re.findall(r'(?:href|src|action)="(/[^"]*)"', page)
        self.assertTrue(links)
        for link in links:
            self.assertTrue(link.startswith("/lab/"), link)
        self.assertIn("/lab/static/style.css", page)
        resp = client.get("/login", base_url=base)
        self.assertTrue(
            all("Path=/lab" in c for c in resp.headers.getlist("Set-Cookie"))
        )
        # redirects and the language switch keep the path
        resp = client.get("/", base_url=base)
        self.assertEqual(resp.headers["Location"], "/lab/login")
        resp = client.get("/lang/en?next=/login", base_url=base)
        self.assertEqual(resp.headers["Location"], "/lab/login")
        # at the root nothing changes
        resp = self.app.test_client().get("/lang/en?next=/login")
        self.assertEqual(resp.headers["Location"], "/login")

    def test_the_remember_cookie_is_limited_to_the_path_too(self):
        add_lab(self.conn)
        client = self.app.test_client()
        base = "http://localhost/lab"
        token = CSRF_RE.search(
            client.get("/login", base_url=base).get_data(as_text=True)
        ).group(1)
        client.post(
            "/login",
            base_url=base,
            data={"email": STUDENT, "csrf_token": token, "remember": "1"},
        )
        code = auth.issue_code(
            self.conn, STUDENT, None, self.cfg["auth"]["code_ttl_minutes"]
        )
        token = CSRF_RE.search(
            client.get("/login/verify", base_url=base).get_data(as_text=True)
        ).group(1)
        resp = client.post(
            "/login/verify",
            base_url=base,
            data={"code": code, "csrf_token": token},
        )
        remember = [
            c
            for c in resp.headers.getlist("Set-Cookie")
            if c.startswith(self.COOKIE)
        ]
        self.assertEqual(len(remember), 1)
        self.assertIn("Path=/lab", remember[0])

    def test_behind_the_proxy_limits_count_the_real_client(self):
        from emptydiff.web import create_app

        cfg = dict(self.cfg, base_url="https://pool.school.invalid/lab")
        cfg["proxy"] = {"addresses": ["10.9.8.7"]}
        cfg["auth"] = dict(cfg["auth"], max_codes_per_ip_hour=1)
        cfg["auth"]["code_cooldown_seconds"] = 0
        app = create_app(cfg)
        client = app.test_client()

        def request_code(email, forwarded, remote="10.9.8.7"):
            """True if a mail with a code was sent."""
            token = CSRF_RE.search(
                client.get("/login").get_data(as_text=True)
            ).group(1)
            with mock.patch.object(mail, "send") as send:
                client.post(
                    "/login",
                    data={"email": email, "csrf_token": token},
                    headers={"X-Forwarded-For": forwarded},
                    environ_overrides={"REMOTE_ADDR": remote},
                )
            return send.called

        self.assertTrue(request_code(STUDENT, "203.0.113.1"))
        # another client behind the same proxy has its own allowance
        self.assertTrue(request_code(OTHER, "203.0.113.2"))
        # the first client may not ask again within the hour
        self.assertFalse(request_code(STUDENT, "203.0.113.1"))
        # a made-up header from someone who is not the proxy is ignored:
        # that client counts with its own address
        self.assertTrue(request_code(STUDENT, "203.0.113.77", "192.0.2.5"))
        self.assertFalse(request_code(OTHER, "203.0.113.78", "192.0.2.5"))

    def test_cookies_are_secure_only_over_https(self):
        from emptydiff.core import config
        from emptydiff.web import create_app

        for url, secure in (
            ("https://emptydiff.school.invalid", True),
            ("http://labserver.local", False),
        ):
            cfg = dict(self.cfg, base_url=url)
            cfg["mail"] = dict(
                cfg["mail"],
                backend="smtp",
                host="smtp.school.invalid",
                sender="emptydiff@school.invalid",
            )
            with (
                self.subTest(url=url),
                mock.patch.object(config, "is_dev", return_value=False),
            ):
                app = create_app(cfg)
                self.assertIs(app.config["SESSION_COOKIE_SECURE"], secure)

    def cookies(self, resp):
        return [c for c in resp.headers.getlist("Set-Cookie")]

    def drop_session(self, client):
        client.delete_cookie(
            "session"
        )  # simulated: browser closed or session expired

    def test_without_consent_there_is_no_persistent_cookie(self):
        add_lab(self.conn)
        client = self.login()
        cookies = self.cookies(self.last_login_response)
        self.assertFalse(any(self.COOKIE in c for c in cookies))
        self.assertTrue(
            all("Expires" not in c and "Max-Age" not in c for c in cookies)
        )  # session cookie
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM remember_token"
            ).fetchone()[0],
            0,
        )
        self.drop_session(client)
        self.assertEqual(client.get("/").status_code, 302)  # logged out

    def test_with_consent_login_survives_losing_the_session(self):
        add_lab(self.conn)
        client = self.login(remember=True)
        cookie = next(
            c
            for c in self.cookies(self.last_login_response)
            if c.startswith(self.COOKIE)
        )
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn(f"Max-Age={180 * 86400}", cookie)
        self.drop_session(client)
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(STUDENT, resp.get_data(as_text=True))
        self.assertEqual(
            self.upload(client, {"addone.m": GOOD}).status_code, 302
        )  # including CSRF after the restore

    def test_consent_applies_only_to_that_login(self):
        self.login(remember=True)
        other = self.login(OTHER, self.app.test_client())
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM remember_token"
            ).fetchone()[0],
            1,
        )
        self.drop_session(other)
        self.assertEqual(other.get("/").status_code, 302)

    def test_logout_revokes_the_token_on_the_server(self):
        client = self.login(remember=True)
        token = client.get_cookie(self.COOKIE).value
        resp = client.post(
            "/logout", data={"csrf_token": self.token(client, "/")}
        )
        self.assertTrue(
            any(c.startswith(f"{self.COOKIE}=;") for c in self.cookies(resp))
        )  # cookie deleted
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM remember_token"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(client.get("/").status_code, 302)
        stolen = (
            self.app.test_client()
        )  # a copied cookie value is useless after logout
        stolen.set_cookie(self.COOKIE, token)
        self.assertEqual(stolen.get("/").status_code, 302)

    def test_invalid_cookie_is_removed(self):
        client = self.app.test_client()
        client.set_cookie(self.COOKIE, "gefaelscht")
        resp = client.get("/login")
        self.assertTrue(
            any(c.startswith(f"{self.COOKIE}=;") for c in self.cookies(resp))
        )
        self.assertEqual(client.get("/").status_code, 302)

    def test_expired_token_does_not_log_in(self):
        client = self.login(remember=True)
        self.conn.execute(
            "UPDATE remember_token SET expires_at = '2000-01-01T00:00:00Z'"
        )
        self.conn.commit()
        self.drop_session(client)
        self.assertEqual(client.get("/").status_code, 302)

    def test_admin_rights_follow_the_config_not_the_token(self):
        client = self.login(ADMIN, remember=True)
        self.drop_session(client)
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertEqual(
            self.login(OTHER, self.app.test_client(), remember=True)
            .get("/admin/")
            .status_code,
            403,
        )


class PrivacyTest(WebTestCase):
    def app_with(self, **privacy):
        from emptydiff.web import create_app

        self.cfg["privacy"].update(privacy)
        return create_app(self.cfg).test_client()

    def test_page_is_public_and_linked_from_every_page(self):
        page = self.client.get("/privacy")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("Datenschutzerklärung", text)
        self.assertIn(
            'href="/privacy">Datenschutz</a>',
            self.client.get("/login").get_data(as_text=True),
        )
        self.assertRegex(
            self.client.get("/login").get_data(as_text=True),
            r'cookie-note">.*<a href="/privacy"',
        )

    def test_missing_contact_data_is_flagged_not_invented(self):
        text = self.client.get("/privacy").get_data(as_text=True)
        self.assertIn("[Angabe fehlt: privacy.controller]", text)
        self.assertIn("[Angabe fehlt: privacy.dpo_email]", text)
        self.assertIn("[Angabe fehlt: privacy.authority]", text)
        self.assertNotIn(
            "i. V. m.", text
        )  # no national supplement without configuration

    def test_values_come_from_the_configuration_and_are_escaped(self):
        client = self.app_with(
            controller="Musterhochschule <b>",
            controller_address="Musterstraße 1\n12345 Musterstadt",
            controller_email="v@example.org",
            dpo_email="d@example.org",
            contact_email="c@example.org",
            authority="Aufsicht <X>",
            authority_url="https://example.org/a",
            legal_basis="§ 3 Testgesetz",
        )
        text = client.get("/privacy").get_data(as_text=True)
        self.assertIn("Musterhochschule &lt;b&gt;", text)
        self.assertIn("12345 Musterstadt<br>", text)
        self.assertIn('href="mailto:d@example.org"', text)
        self.assertIn(
            '<a href="https://example.org/a" '
            'rel="noopener">Aufsicht &lt;X&gt;</a>',
            text,
        )
        self.assertIn("i. V. m. § 3 Testgesetz", text)
        self.assertNotIn("Angabe fehlt", text)

    def test_text_reflects_the_actual_settings(self):
        self.cfg["retention_days"] = 730
        self.cfg["auth"]["remember_days"] = 90
        client = self.app_with(log_days=7)
        text = client.get("/privacy").get_data(as_text=True)
        self.assertIn("730 Tage (rund 2.0 Jahre)", text)
        self.assertIn("höchstens 90 Tage ohne Nutzung", text)
        self.assertIn("nach 7 Tagen gelöscht", text)
        self.assertIn("emptydiff_remember", text)

    def test_imprint_link_only_for_safe_urls(self):
        self.assertNotIn(
            "Impressum", self.client.get("/login").get_data(as_text=True)
        )
        ok = self.app_with(imprint_url="https://example.org/impressum")
        self.assertIn(
            'href="https://example.org/impressum">Impressum',
            ok.get("/login").get_data(as_text=True),
        )
        bad = self.app_with(imprint_url="javascript:alert(1)")
        self.assertNotIn(
            "javascript:", bad.get("/login").get_data(as_text=True)
        )


class BackupWebTest(WebTestCase):
    def setUp(self):
        super().setUp()
        add_lab(self.conn, "am26-1a", active=1, provided=GAUSS_FUNCTION)
        add_lab(self.conn, "am26-1b", active=0)
        self.admin = self.login(ADMIN, self.app.test_client())
        self.student = self.login(STUDENT, self.app.test_client())
        self.upload(self.student, {"addone.m": GOOD}, lab="am26-1a")
        worker.run_batch(self.conn, self.cfg)
        self.staging = os.path.join(self.cfg["data_dir"], "import-staging")

    def post(self, path, data=None, client=None, page="/admin/backup", **kw):
        client = client or self.admin
        data = dict(data or {})
        data["csrf_token"] = self.token(client, page)
        return client.post(path, data=data, **kw)

    def download(self, kind="full.zip"):
        resp = self.admin.get(f"/admin/backup/{kind}")
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def upload_archive(self, data, client=None, name="backup.zip"):
        client = client or self.admin
        return client.post(
            "/admin/backup/upload",
            content_type="multipart/form-data",
            data={
                "csrf_token": self.token(client, "/admin/backup"),
                "archive": (io.BytesIO(data), name),
            },
        )

    def staged(self):
        return os.listdir(self.staging) if os.path.isdir(self.staging) else []

    def token_of(self, resp):
        return re.search(
            r'name="token" value="([0-9a-f]{32})"', resp.get_data(as_text=True)
        ).group(1)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # --- Access ---
    def test_only_admins_have_access(self):
        paths = [
            "/admin/backup",
            "/admin/backup/full.zip",
            "/admin/backup/labs.zip",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(
                    self.app.test_client().get(path).status_code, 302
                )
                self.assertEqual(self.student.get(path).status_code, 403)
        for path in (
            "/admin/backup/upload",
            "/admin/backup/apply",
            "/admin/backup/cancel",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.post(path, client=self.student, page="/").status_code,
                    403,
                )
                self.assertEqual(
                    self.admin.post(path).status_code, 400
                )  # without CSRF token

    def test_nav_tab_and_page(self):
        page = self.admin.get("/admin/backup").get_data(as_text=True)
        self.assertIn('aria-current="page">Datensicherung', page)
        self.assertIn("personenbezogene Daten", page)
        self.assertIn("500 MB", page)

    # --- Download ---
    def test_full_download(self):
        resp = self.admin.get("/admin/backup/full.zip")
        self.assertEqual(resp.mimetype, "application/zip")
        self.assertRegex(
            resp.headers["Content-Disposition"],
            r'attachment; filename="emptydiff-backup-\d{8}-\d{6}\.zip"',
        )
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(int(resp.headers["Content-Length"]), len(resp.data))
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(resp.data)).read("manifest.json")
        )
        self.assertEqual(
            (manifest["kind"], manifest["counts"]["submissions"]), ("full", 1)
        )
        self.assertEqual(
            os.listdir(os.path.join(self.cfg["data_dir"], "tmp")), []
        )  # nothing is left behind

    def test_labs_download_has_no_submissions(self):
        data = self.download("labs.zip")
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertFalse(any(n.startswith("submissions") for n in names))

    # --- Upload and check ---
    def test_invalid_uploads_are_rejected_and_not_kept(self):
        self.assertIn(
            "kein gültiges ZIP",
            self.upload_archive(b"kein zip", name="x.zip").status_code == 302
            and self.admin.get("/admin/backup").get_data(as_text=True),
        )
        self.assertEqual(self.staged(), [])
        resp = self.admin.post(
            "/admin/backup/upload",
            content_type="multipart/form-data",
            data={"csrf_token": self.token(self.admin, "/admin/backup")},
        )
        self.assertEqual(resp.status_code, 302)  # no file chosen

    def test_upload_shows_a_summary_and_requires_the_confirmation_word(self):
        data = self.download()
        resp = self.upload_archive(data)
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True)
        self.assertIn("Vollbackup wiederherstellen", text)
        self.assertIn("1 (mit 1 Dateien)", text)
        self.assertIn("unwiderruflich ersetzt", text)
        token = self.token_of(resp)
        self.assertEqual(len(self.staged()), 1)

        wrong = self.post(
            "/admin/backup/apply",
            {"token": token, "confirm": "ja"},
            page="/admin/backup",
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("ERSETZEN", wrong.get_data(as_text=True))
        self.assertEqual(
            len(self.staged()), 1
        )  # keeps waiting for the confirmation

    def test_full_restore_replaces_everything(self):
        data = self.download()
        # after the backup: change the state
        add_lab(self.conn, "am99-9z")
        self.conn.execute("DELETE FROM submission")
        self.conn.commit()
        shutil.rmtree(os.path.join(self.cfg["data_dir"], "submissions"))
        resp = self.upload_archive(data)
        token = self.token_of(resp)
        done = self.post(
            "/admin/backup/apply",
            {"token": token, "confirm": " ERSETZEN "},
            follow_redirects=True,
        )
        text = done.get_data(as_text=True)
        self.assertIn("Wiederhergestellt: 2 Versuche, 1 Abgaben", text)
        self.assertEqual(
            [
                r["name"]
                for r in self.conn.execute(
                    "SELECT name FROM lab ORDER BY name"
                )
            ],
            ["am26-1a", "am26-1b"],
        )
        self.assertEqual(self.count("submission"), 1)
        self.assertEqual(self.staged(), [])
        page = self.student.get("/lab/am26-1a").get_data(
            as_text=True
        )  # student sees their submission again
        self.assertIn("Abgabe 1", page)
        self.assertEqual(
            self.student.get("/").status_code, 200
        )  # login persists

    def test_labs_archive_flow(self):
        data = self.download("labs.zip")
        self.conn.execute("DELETE FROM submission")
        self.conn.execute("DELETE FROM lab WHERE name = 'am26-1b'")
        self.conn.execute(
            "UPDATE lab SET description = 'geändert' WHERE name = 'am26-1a'"
        )
        self.conn.commit()
        resp = self.upload_archive(data)
        text = resp.get_data(as_text=True)
        self.assertIn("Versuche übernehmen", text)
        self.assertIn("1 neue Versuche", text)
        self.assertIn("inaktiv", text)
        self.assertIn("1 vorhandene Versuche werden überschrieben", text)
        self.assertNotIn("ERSETZEN", text)
        done = self.post(
            "/admin/backup/apply",
            {"token": self.token_of(resp)},
            follow_redirects=True,
        )
        self.assertIn(
            "1 neu (inaktiv), 1 überschrieben", done.get_data(as_text=True)
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT description FROM lab WHERE name = 'am26-1a'"
            ).fetchone()[0],
            "Beschreibung am26-1a",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT active FROM lab WHERE name = 'am26-1b'"
            ).fetchone()[0],
            0,
        )

    def test_labs_archive_never_replaces_submissions(self):
        data = self.download("labs.zip")
        resp = self.upload_archive(data)
        self.post("/admin/backup/apply", {"token": self.token_of(resp)})
        self.assertEqual(self.count("submission"), 1)

    def test_tokens_cannot_point_elsewhere(self):
        for token in ("../../secret_key", "x" * 32, "", "0" * 32, "A" * 32):
            with self.subTest(token=token):
                resp = self.post(
                    "/admin/backup/apply",
                    {"token": token, "confirm": "ERSETZEN"},
                    follow_redirects=True,
                )
                self.assertIn(
                    "nicht mehr vorhanden", resp.get_data(as_text=True)
                )
        self.assertEqual(self.count("lab"), 2)

    def test_cancel_deletes_the_staged_file(self):
        token = self.token_of(self.upload_archive(self.download()))
        self.assertEqual(len(self.staged()), 1)
        self.post("/admin/backup/cancel", {"token": token})
        self.assertEqual(self.staged(), [])
        self.post(
            "/admin/backup/cancel", {"token": "../../x"}
        )  # no error, nothing outside affected

    def test_unconfirmed_uploads_expire(self):
        self.upload_archive(self.download())
        (name,) = self.staged()
        old = time.time() - 2 * 3600
        os.utime(os.path.join(self.staging, name), (old, old))
        self.admin.get("/admin/backup")
        self.assertEqual(self.staged(), [])

    def test_apply_fails_cleanly_if_the_archive_became_invalid(self):
        token = self.token_of(self.upload_archive(self.download()))
        with open(os.path.join(self.staging, token + ".zip"), "wb") as f:
            f.write(b"ausgetauscht")
        resp = self.post(
            "/admin/backup/apply",
            {"token": token, "confirm": "ERSETZEN"},
            follow_redirects=True,
        )
        self.assertIn("kein gültiges ZIP", resp.get_data(as_text=True))
        self.assertEqual(self.count("lab"), 2)
        self.assertEqual(self.staged(), [])

    # --- Size ---
    def big_archive(self):
        add_lab(self.conn, "am26-9z")
        for i in range(3):
            email = f"gross{i}@student.example.edu"
            files = {"addone.m": os.urandom(2_400_000)}
            self.conn.execute(
                "INSERT INTO submission (lab, email, attempt, "
                "created_at, status) VALUES ('am26-9z', ?, 1, "
                "'2026-10-01T12:00:00Z', 'done')",
                (email,),
            )
            d = submissions.submission_dir(self.cfg, "am26-9z", email, 1)
            os.makedirs(d)
            with open(os.path.join(d, "addone.m"), "wb") as f:
                f.write(files["addone.m"])
        self.conn.commit()
        return self.download()

    def test_admins_may_upload_large_archives_others_may_not(self):
        data = self.big_archive()
        self.assertGreater(
            len(data), self.cfg["limits"]["max_upload_bytes"] + 200_000
        )
        with mock.patch(
            "tempfile.TemporaryFile", wraps=tempfile.TemporaryFile
        ) as spy:
            resp = self.upload_archive(data)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            any(
                c.kwargs.get("dir", "").endswith("tmp")
                for c in spy.call_args_list
            )
        )  # not in /tmp
        self.assertEqual(
            self.upload_archive(data, client=self.student).status_code, 413
        )  # students: normal limit
        self.assertEqual(
            self.app.test_client()
            .post("/admin/backup/upload", data=data)
            .status_code,
            413,
        )

    def test_upload_limit_is_configurable(self):
        data = self.big_archive()
        from emptydiff.web import create_app

        self.cfg["limits"]["max_backup_bytes"] = self.cfg["limits"][
            "max_upload_bytes"
        ]
        admin = self.login(ADMIN, create_app(self.cfg).test_client())
        self.assertEqual(
            self.upload_archive(data, client=admin).status_code, 413
        )


class AdminTest(WebTestCase):
    LAB_FORM = {
        "description": "Aufgabentext",
        "due_at": "2099-03-01T23:59",
        "required_files": "addone.m",
        "show_report": "1",
        "testbench": "check(true, 'ok', 'falsch');\n",
        "helpers": "",
    }

    def setUp(self):
        super().setUp()
        add_lab(self.conn, "am26-1a", active=1)
        add_lab(self.conn, "am26-1b", active=0)
        add_lab(self.conn, "am25-1a", active=0)
        self.admin = self.login(ADMIN, self.app.test_client())

    def post(self, path, data=None, client=None, page="/admin/", **kw):
        client = client or self.admin
        data = dict(data or {})
        data["csrf_token"] = self.token(client, page)
        return client.post(path, data=data, **kw)

    def active(self):
        return {
            r["name"]
            for r in self.conn.execute("SELECT name FROM lab WHERE active = 1")
        }

    # --- Access ---
    def test_anonymous_is_redirected_and_students_are_forbidden(self):
        student = self.login(STUDENT, self.app.test_client())
        paths = [
            "/admin/",
            "/admin/course/new",
            "/admin/course/am26/lab/new",
            "/admin/lab/am26-1a/edit",
            "/admin/lab/am26-1a/submissions",
            "/admin/course/am26/clone",
            "/admin/credit",
            "/admin/credit.csv",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(
                    self.app.test_client().get(path).status_code, 302
                )
                self.assertEqual(student.get(path).status_code, 403)
        for path in (
            "/admin/lab/am26-1b/active",
            "/admin/lab/am26-1a/delete",
            "/admin/submission/1/delete",
            "/admin/submission/1/regrade",
            "/admin/course/new",
            "/admin/course/am26/lab/new",
            "/admin/course/am25/delete",
        ):
            with self.subTest(path=path):
                resp = self.post(
                    path, {"active": "1"}, client=student, page="/"
                )
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.active(), {"am26-1a"})

    def test_login_with_capital_letters_works_and_admins_are_recognized(self):
        client = self.app.test_client()
        client.post(
            "/login",
            data={
                "email": "  " + ADMIN.upper() + " ",
                "csrf_token": self.token(client),
            },
        )
        body = next(
            m["body"]
            for m in reversed(mail.SIMULATED)
            if m["recipient"] == ADMIN
        )  # the mail goes to the lower-case address
        code = CODE_RE.search(body).group(1)
        resp = client.post(
            "/login/verify",
            data={
                "code": code,
                "csrf_token": self.token(client, "/login/verify"),
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(client.get("/admin/").status_code, 200)

    def test_admin_link_only_for_admins(self):
        student = self.login(STUDENT, self.app.test_client())
        self.assertNotIn("/admin/", student.get("/").get_data(as_text=True))
        self.assertIn(
            'href="/admin/"', self.admin.get("/").get_data(as_text=True)
        )

    def test_post_needs_csrf_token(self):
        resp = self.admin.post(
            "/admin/lab/am26-1b/active", data={"active": "1"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.active(), {"am26-1a"})

    # --- Activating ---
    def test_index_groups_courses_and_shows_state(self):
        page = self.admin.get("/admin/").get_data(as_text=True)
        self.assertLess(
            page.index('id="am26"'), page.index('id="am25"')
        )  # newest course first
        self.assertIn("1 von 2 aktiv", page)
        self.assertIn("0 von 1 aktiv", page)
        self.assertIn(
            "<b>1</b> von 3 Versuchen sind für Studierende sichtbar", page
        )
        self.assertEqual(page.count('aria-checked="true"'), 1)
        self.assertEqual(page.count('aria-checked="false"'), 2)

    def test_open_courses_are_those_with_active_labs(self):
        page = self.admin.get("/admin/").get_data(as_text=True)
        self.assertRegex(page, r'<details class="course card" id="am26" open>')
        self.assertRegex(page, r'<details class="course card" id="am25">')
        page = self.admin.get("/admin/?open=am25").get_data(as_text=True)
        self.assertRegex(page, r'<details class="course card" id="am25" open>')

    def test_toggle_lab_changes_what_students_see(self):
        student = self.login(STUDENT, self.app.test_client())
        self.assertNotIn("am26-1b", student.get("/").get_data(as_text=True))
        resp = self.post("/admin/lab/am26-1b/active", {"active": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("#am26", resp.headers["Location"])
        self.assertEqual(self.active(), {"am26-1a", "am26-1b"})
        self.assertIn("am26-1b", student.get("/").get_data(as_text=True))
        self.post("/admin/lab/am26-1a/active", {"active": "0"})
        page = student.get("/").get_data(as_text=True)
        self.assertNotIn("am26-1a", page)
        self.assertEqual(student.get("/lab/am26-1a").status_code, 404)

    def test_toggle_unknown_lab_is_404(self):
        self.assertEqual(
            self.post(
                "/admin/lab/am99-1a/active", {"active": "1"}
            ).status_code,
            404,
        )

    def upload_to(self, client, lab, files):
        data = {
            "csrf_token": self.token(client, f"/lab/{lab}"),
            "files": [
                (io.BytesIO(content), name) for name, content in files.items()
            ],
        }
        return client.post(
            f"/lab/{lab}", data=data, content_type="multipart/form-data"
        )

    def test_admin_can_try_out_a_hidden_lab_but_students_cannot(self):
        self.assertEqual(self.admin.get("/lab/am26-1b").status_code, 200)
        resp = self.upload_to(self.admin, "am26-1b", {"addone.m": GOOD})
        self.assertEqual(resp.status_code, 302)
        self.assertRegex(resp.headers["Location"], r"/submission/\d+$")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM submission WHERE lab = 'am26-1b'"
            ).fetchone()[0],
            1,
        )
        student = self.login(STUDENT, self.app.test_client())
        self.assertEqual(student.get("/lab/am26-1b").status_code, 404)
        self.assertEqual(
            self.upload_to(student, "am26-1a", {"addone.m": GOOD}).status_code,
            302,
        )  # a released lab works
        self.assertEqual(
            self.upload_to(student, "am26-1b", {"addone.m": GOOD}).status_code,
            404,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM submission WHERE lab = 'am26-1b'"
            ).fetchone()[0],
            1,
        )

    def test_hidden_lab_is_clearly_marked_for_admins_only(self):
        hidden = self.admin.get("/lab/am26-1b").get_data(as_text=True)
        self.assertIn('class="preview-banner"', hidden)
        self.assertIn("noch verborgen", hidden)
        self.assertNotIn(
            "/admin/", hidden.split("<main")[1]
        )  # no detour through the administration
        released = self.admin.get("/lab/am26-1a").get_data(as_text=True)
        self.assertNotIn("preview-banner", released)
        student = self.login(STUDENT, self.app.test_client())
        self.assertNotIn(
            "preview-banner",
            student.get("/lab/am26-1a").get_data(as_text=True),
        )

    def test_admin_sees_all_labs_and_hidden_ones_are_marked(
        self,
    ):
        overview = self.admin.get("/").get_data(as_text=True)
        for name in ("am26-1a", "am26-1b", "am25-1a"):
            self.assertIn(f'<a href="/lab/{name}">{name}</a>', overview)
        self.assertRegex(
            overview,
            r'am26-1b</a> <span class="badge hidden '
            r'tip"[^>]*aria-label="verborgen" title="Verborgen: '
            r"Studierende sehen",
        )
        self.assertNotIn('am26-1a</a> <span class="badge hidden', overview)
        student = self.login(STUDENT, self.app.test_client())
        page = student.get("/").get_data(as_text=True)
        self.assertIn("am26-1a", page)
        self.assertNotIn("am26-1b", page)

    def test_banner_on_the_submission_page_of_a_hidden_lab(self):
        resp = self.upload_to(self.admin, "am26-1b", {"addone.m": GOOD})
        page = self.admin.get(resp.headers["Location"]).get_data(as_text=True)
        self.assertIn('class="preview-banner"', page)

    def test_hidden_lab_has_the_attempt_limit_of_students(self):
        self.cfg["limits"]["max_attempts"] = 1
        self.assertEqual(
            self.upload_to(
                self.admin, "am26-1b", {"addone.m": GOOD}
            ).status_code,
            302,
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done' WHERE lab = 'am26-1b'"
        )
        self.conn.commit()
        self.assertNotIn(
            'type="file"',
            self.admin.get("/lab/am26-1b").get_data(as_text=True),
        )

    def test_admin_sees_own_submissions_like_a_student(
        self,
    ):
        lab = "am26-1a"
        self.conn.execute(
            "UPDATE lab SET show_report = 0 WHERE name = ?", (lab,)
        )
        self.conn.commit()
        student = self.login(STUDENT, self.app.test_client())
        own_id = int(
            self.upload_to(self.admin, lab, {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        other_id = int(
            self.upload_to(student, lab, {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        for sid in (own_id, other_id):
            self.conn.execute(
                "UPDATE submission SET status = 'done', passed = 1, "
                "checks = '[]', finished = 1 WHERE id = ?",
                (sid,),
            )
        self.conn.commit()
        own = self.admin.get(f"/submission/{own_id}").get_data(as_text=True)
        other = self.admin.get(f"/submission/{other_id}").get_data(
            as_text=True
        )
        # the own submission: files like a student sees them, but
        # none of what only lecturers get
        self.assertIn("Abgegebene Dateien", own)
        self.assertNotIn("Alle Dateien (ZIP)", own)
        self.assertIn("Alle Dateien (ZIP)", other)

    def test_admin_view_shows_files_of_an_own_submission(self):
        lab = "am26-1a"
        self.conn.execute(
            "UPDATE lab SET show_report = 0 WHERE name = ?", (lab,)
        )
        self.conn.commit()
        sid = int(
            self.upload_to(self.admin, lab, {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = 1, "
            "checks = '[[1, \"ok\"]]', finished = 1 WHERE id = ?",
            (sid,),
        )
        self.conn.commit()
        student_view = self.admin.get(f"/submission/{sid}").get_data(
            as_text=True
        )
        self.assertIn("Abgegebene Dateien", student_view)
        self.assertNotIn("Testbench des Versuchs", student_view)
        admin_view = self.admin.get(f"/admin/submission/{sid}").get_data(
            as_text=True
        )
        self.assertIn("Abgegebene Dateien", admin_view)
        self.assertIn("addone.m", admin_view)
        self.assertIn(
            f'href="/admin/lab/{lab}/submissions"', admin_view
        )  # back to the administration, not to the student page
        self.assertIn("Student: dozent@example.edu", admin_view)

    def test_admin_view_shows_testbench_helpers_provided_and_output(self):
        self.conn.execute(
            "UPDATE lab SET show_report = 0, helpers = ?, provided = ?, "
            "testbench = ? WHERE name = 'am26-1a'",
            (
                "function h()\n  disp(1);\nend\n",
                GAUSS_FUNCTION,
                "% marker-testbench\ncheck(true, 'ok', 'falsch');\n",
            ),
        )
        self.conn.commit()
        sid = int(
            self.upload_to(self.admin, "am26-1a", {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = 0, "
            'checks = \'[[1, "a"], [0, "b"]]\', finished = 0, '
            "timed_out = 1, log = 'Ausgabe-Zeile', "
            "error_log = 'error: Fehler-Zeile' WHERE id = ?",
            (sid,),
        )
        self.conn.commit()
        page = self.admin.get(f"/admin/submission/{sid}").get_data(
            as_text=True
        )
        self.assertIn("Testbench des Versuchs", page)
        self.assertIn("testbench.m", page)
        self.assertIn("helpers.m", page)
        self.assertIn("provided.m", page)
        self.assertIn("marker-testbench", page)
        self.assertIn(
            '<span class="tok-comment">% marker-testbench</span>', page
        )
        self.assertIn('<span class="tok-keyword">function</span>', page)
        self.assertIn("/admin/lab/am26-1a/edit", page)
        # the raw output, although students only get a receipt
        self.assertIn("Ausgabe der Prüfung", page)
        self.assertIn("Aufgezeichnete Prüfungen", page)
        self.assertIn("[OK] a\n[FAIL] b", page)
        self.assertIn("Ausgabe-Zeile", page)
        self.assertIn("error: Fehler-Zeile", page)
        self.assertRegex(page, r"Zeitlimit überschritten</dt><dd>ja</dd>")
        self.assertIn("1 von 2 bestanden", page)
        # the student view of the same submission shows none of it
        student_view = self.admin.get(f"/submission/{sid}").get_data(
            as_text=True
        )
        for text in ("Testbench des Versuchs", "Ausgabe der Prüfung"):
            self.assertNotIn(text, student_view)

    def test_admin_view_without_a_finished_check_shows_no_output(self):
        sid = int(
            self.upload_to(self.admin, "am26-1a", {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        self.conn.execute(
            "UPDATE submission SET status = 'queued' WHERE id = ?", (sid,)
        )
        self.conn.commit()
        page = self.admin.get(f"/admin/submission/{sid}").get_data(
            as_text=True
        )
        self.assertIn("Testbench des Versuchs", page)
        self.assertNotIn("Ausgabe der Prüfung", page)

    def test_lab_list_polls_only_while_a_check_is_pending(self):
        sid = int(
            self.upload_to(self.admin, "am26-1a", {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        path = "/admin/lab/am26-1a/submissions"
        pending = self.admin.get(path).get_data(as_text=True)
        self.assertIn('data-poll="list"', pending)
        self.assertIn('<noscript><meta http-equiv="refresh"', pending)
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = 1, "
            "checks = '[]', finished = 1 WHERE id = ?",
            (sid,),
        )
        self.conn.commit()
        done = self.admin.get(path).get_data(as_text=True)
        self.assertNotIn("data-poll", done)
        self.assertNotIn("http-equiv", done)

    def test_credit_list_and_lab_list_link_to_the_admin_view(self):
        sid = int(
            self.upload_to(self.admin, "am26-1a", {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = 1, "
            "checks = '[]', finished = 1 WHERE id = ?",
            (sid,),
        )
        self.conn.commit()
        credit = self.admin.get("/admin/credit").get_data(as_text=True)
        self.assertIn(f'href="/admin/submission/{sid}"', credit)
        self.assertNotIn(f'href="/submission/{sid}"', credit)
        listing = self.admin.get("/admin/lab/am26-1a/submissions").get_data(
            as_text=True
        )
        self.assertIn(f'href="/admin/submission/{sid}"', listing)

    def test_index_shows_students_of_the_lab_per_students_of_the_course(self):
        student = self.login(STUDENT, self.app.test_client())
        self.upload_to(student, "am26-1a", {"addone.m": GOOD})
        self.upload_to(self.admin, "am26-1b", {"addone.m": GOOD})  # a test
        page = self.admin.get("/admin/").get_data(as_text=True)
        # one student in 1a of one student in the course; the lecturer
        # does not count; 1b has none
        self.assertRegex(
            page,
            r'(?s)am26-1a.*?<span title="[^"]*">1 / 1</span>.*?'
            r'am26-1b.*?<span title="[^"]*">0 / 1</span>',
        )
        self.assertIn("Abgegeben von", page)

    def test_lab_list_groups_by_student_with_the_latest_submission_first(self):
        student = self.login(STUDENT, self.app.test_client())
        ids = []
        for client in (student, student, self.admin):
            sid = int(
                self.upload_to(client, "am26-1a", {"addone.m": GOOD})
                .headers["Location"]
                .rsplit("/", 1)[1]
            )
            self.conn.execute(
                "UPDATE submission SET status = 'done', passed = 1, "
                "checks = '[]', finished = 1 WHERE id = ?",
                (sid,),
            )
            self.conn.commit()
            ids.append(sid)
        page = self.admin.get("/admin/lab/am26-1a/submissions").get_data(
            as_text=True
        )
        self.assertEqual(page.count('<tbody class="group'), 2)  # students
        groups = page.split('<tbody class="group')[1:]
        # alphabetical by address; per group the latest comes first
        self.assertIn("dozent@example.edu", groups[0])
        self.assertNotIn('class="older"', groups[0])  # one attempt: no fold
        self.assertNotIn("data-group-toggle", groups[0])
        self.assertIn("max.muster@student.example.edu", groups[1])
        self.assertIn("collapsed", groups[1].split(">")[0])
        self.assertIn("1 frühere Abgabe", groups[1])
        self.assertIn("data-group-toggle", groups[1])
        latest, older = groups[1].split('<tr class="older">')
        self.assertIn(f"/admin/submission/{ids[1]}", latest)
        self.assertNotIn(f"/admin/submission/{ids[0]}", latest)
        self.assertIn(f"/admin/submission/{ids[0]}", older)
        self.assertIn("frühere Abgabe", older)

    def test_admin_view_is_for_admins_only(self):
        sid = int(
            self.upload_to(self.admin, "am26-1a", {"addone.m": GOOD})
            .headers["Location"]
            .rsplit("/", 1)[1]
        )
        student = self.login(STUDENT, self.app.test_client())
        self.assertEqual(
            student.get(f"/admin/submission/{sid}").status_code, 403
        )
        self.assertEqual(
            self.app.test_client().get(f"/admin/submission/{sid}").status_code,
            302,
        )
        self.assertEqual(
            self.admin.get("/admin/submission/999").status_code, 404
        )

    # --- Create, edit, delete ---
    def create(self, ident="1a", course="am27", **over):
        if not labs.course_exists(self.conn, course):
            labs.create_course(self.conn, course)
        path = f"/admin/course/{course}/lab/new"
        return self.post(
            path, {**self.LAB_FORM, "ident": ident, **over}, page=path
        )

    def test_create_lab_with_provided_functions(self):
        labs.create_course(self.conn, "am27")
        resp = self.create(ident="1a", course="am27", provided=GAUSS_FUNCTION)
        self.assertEqual(resp.status_code, 302)
        lab = self.conn.execute(
            "SELECT * FROM lab WHERE name = 'am27-1a'"
        ).fetchone()
        self.assertEqual(
            (lab["course"], lab["active"]), ("am27", 0)
        )  # new labs are hidden
        self.assertEqual(lab["due_at"], "2099-03-01T22:59:59Z")
        self.assertEqual(lab["provided"], GAUSS_FUNCTION)

    # --- Language of a lab ---
    def test_lab_form_offers_the_languages_of_the_server(
        self,
    ):
        page = self.admin.get("/admin/course/am26/lab/new").get_data(
            as_text=True
        )
        self.assertRegex(
            page,
            r'<select id="f-language" name="language" '
            r"data-language-select>\s*<option "
            r'value="octave"[^>]*selected>Octave/MATLAB \(\.m\)</option>'
            r"\s*</select>",
        )
        self.assertRegex(
            page, r'<div class="explain" data-language-block="octave">'
        )
        self.assertIn('data-highlight="octave"', page)
        self.assertNotIn(
            "Octave-Programm", page
        )  # the generic texts do not name a language

    @with_toy
    def test_lab_form_with_two_languages_hides_the_help_of_the_other_one(self):
        page = self.admin.get("/admin/course/am26/lab/new").get_data(
            as_text=True
        )
        self.assertIn('<option value="toy"', page)
        self.assertRegex(page, r'data-language-block="toy" hidden')
        self.assertNotRegex(page, r'data-language-block="octave" hidden')

    @with_toy
    def test_next_lab_of_the_course_gets_the_same_language(
        self,
    ):
        labs.create_course(self.conn, "am27")
        resp = self.create(
            ident="1a", course="am27", language="toy", required_files="a.toy"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(labs.get(self.conn, "am27-1a")["language"], "toy")
        page = self.admin.get("/admin/course/am27/lab/new").get_data(
            as_text=True
        )
        self.assertRegex(page, r'<option value="toy"[^>]*selected>')

    def test_unknown_language_is_refused_with_a_message(self):
        labs.create_course(self.conn, "am27")
        resp = self.create(ident="1a", course="am27", language="nonsense")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            "wird von diesem System nicht angeboten",
            resp.get_data(as_text=True),
        )
        self.assertIsNone(labs.get(self.conn, "am27-1a"))

    @with_toy
    def test_language_is_locked_once_there_are_submissions(
        self,
    ):
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertNotRegex(
            page, r"data-language-select disabled"
        )  # nothing submitted yet: the language can still be changed
        self.upload_to(self.admin, "am26-1a", {"addone.m": GOOD})
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertRegex(
            page,
            r'<select id="f-language" name="language" '
            r"data-language-select disabled>",
        )
        self.assertRegex(
            page, r'<input type="hidden" name="language" value="octave">'
        )
        resp = self.post(
            "/admin/lab/am26-1a/edit",
            {**self.LAB_FORM, "language": "toy", "required_files": "a.toy"},
            page="/admin/lab/am26-1a/edit",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            "nicht mehr geändert werden", resp.get_data(as_text=True)
        )
        self.assertEqual(labs.get(self.conn, "am26-1a")["language"], "octave")

    def test_new_form_is_inactive_by_default(self):
        page = self.admin.get("/admin/course/am26/lab/new").get_data(
            as_text=True
        )
        self.assertIn(
            "am26-</span>", page
        )  # the course is fixed, only the identifier is typed
        self.assertNotRegex(page, r'name="active"[^>]* checked')
        self.assertIn("check(addone(1) == 2", page)  # template with an example
        self.assertIn('name="helpers"', page)

    def test_create_errors_keep_input_and_show_message(self):
        for over, message in (
            ({"ident": "fal sch"}, "Die Kennung darf"),
            ({"due_at": ""}, "Frist"),
            ({"required_files": "../x"}, "Ungültiger Dateiname"),
            ({"course": "am26", "ident": "1a"}, "gibt es schon"),
        ):
            with self.subTest(over=over):
                resp = self.create(**over)
                self.assertEqual(resp.status_code, 400)
                page = resp.get_data(as_text=True)
                self.assertIn(message, page)
                self.assertIn("Aufgabentext", page)  # input is not lost
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM lab").fetchone()[0], 3
        )

    def test_edit_lab(self):
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertIn("Beschreibung am26-1a", page)
        self.assertIn(
            'value="2099-01-01T23:59"', page
        )  # local time of the deadline
        resp = self.post(
            "/admin/lab/am26-1a/edit",
            {**self.LAB_FORM, "description": "Neu"},
            page="/admin/lab/am26-1a/edit",
        )
        self.assertEqual(resp.status_code, 302)
        lab = self.conn.execute(
            "SELECT * FROM lab WHERE name = 'am26-1a'"
        ).fetchone()
        self.assertEqual(
            (lab["active"], lab["description"]), (1, "Neu")
        )  # the form does not change the visibility

    def test_the_lab_form_has_no_visibility_checkbox_and_ignores_a_posted_one(
        self,
    ):
        for path in ("/admin/lab/am26-1b/edit", "/admin/course/am26/lab/new"):
            page = self.admin.get(path).get_data(as_text=True)
            self.assertNotIn('name="active"', page)
            self.assertNotIn("Sichtbarkeit", page)
        self.post(
            "/admin/lab/am26-1b/edit",
            {**self.LAB_FORM, "active": "1"},
            page="/admin/lab/am26-1b/edit",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT active FROM lab WHERE name = 'am26-1b'"
            ).fetchone()[0],
            0,
        )

    def test_students_see_which_functions_are_provided(self):
        client = self.login(STUDENT, self.app.test_client())
        self.conn.execute(
            "UPDATE lab SET provided = ? WHERE name = 'am26-1a'",
            (GAUSS_FUNCTION,),
        )
        self.conn.commit()
        page = client.get("/lab/am26-1a").get_data(as_text=True)
        self.assertRegex(
            page,
            r"<dt>Bereitgestellt</dt>\s*<dd><span "
            r'class="chips"><code class="chip">gauss.m</code>',
        )

    def test_provided_functions_are_shown_edited_and_checked(self):
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertIn("Bereitgestellte Funktionen (optional)", page)
        self.assertRegex(
            page, r'<textarea id="f-provided"[^>]* data-highlight'
        )
        self.assertNotIn(
            'type="file"', page
        )  # no more upload of provided files
        self.assertNotIn(
            "multipart", page.split("<form", 2)[2].split(">", 1)[0]
        )
        self.post(
            "/admin/lab/am26-1a/edit",
            {
                **self.LAB_FORM,
                "provided": GAUSS_FUNCTION
                + "\n"
                + GAUSS_FUNCTION.replace("gauss", "gauss2"),
            },
            page="/admin/lab/am26-1a/edit",
        )
        self.assertIn(
            "gauss2",
            self.conn.execute(
                "SELECT provided FROM lab WHERE name = 'am26-1a'"
            ).fetchone()[0],
        )
        resp = self.post(
            "/admin/lab/am26-1a/edit",
            {**self.LAB_FORM, "provided": "x = 1;\n"},
            page="/admin/lab/am26-1a/edit",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("function", resp.get_data(as_text=True))
        self.assertIn(
            "x = 1;", resp.get_data(as_text=True)
        )  # the input is not lost

    def test_save_warns_about_bad_testbench(self):
        resp = self.post(
            "/admin/lab/am26-1a/edit",
            {**self.LAB_FORM, "testbench": "disp('x')"},
            page="/admin/lab/am26-1a/edit",
            follow_redirects=True,
        )
        self.assertIn("keinen Aufruf von check", resp.get_data(as_text=True))

    def test_old_style_testbench_is_refused_with_a_hint(self):
        old = (
            "function [] = testbench()\n  disp('[Verification ready]');\nend\n"
        )
        resp = self.post(
            "/admin/lab/am26-1a/edit",
            {**self.LAB_FORM, "testbench": old},
            page="/admin/lab/am26-1a/edit",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Hilfsfunktionen", resp.get_data(as_text=True))

    def test_code_textareas_are_prepared_for_highlighting(self):
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertRegex(page, r'<textarea id="f-tb"[^>]* data-highlight')
        self.assertRegex(page, r'<textarea id="f-helpers"[^>]* data-highlight')
        self.assertNotRegex(page, r'<textarea id="f-req"[^>]* data-highlight')

    def test_helpers_are_saved_and_shown(self):
        helpers = "function y = twice(x)\n  y = 2 * x;\nend"
        self.post(
            "/admin/lab/am26-1a/edit",
            {**self.LAB_FORM, "helpers": helpers},
            page="/admin/lab/am26-1a/edit",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT helpers FROM lab WHERE name = 'am26-1a'"
            ).fetchone()[0],
            helpers + "\n",
        )
        self.assertIn(
            "function y = twice(x)",
            self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True),
        )

    def test_description_is_escaped(self):
        self.post(
            "/admin/lab/am26-1a/edit",
            {
                **self.LAB_FORM,
                "description": "</textarea><script>alert(1)</script>",
            },
            page="/admin/lab/am26-1a/edit",
        )
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        student = self.login(STUDENT, self.app.test_client())
        self.assertNotIn(
            "<script>alert(1)</script>",
            student.get("/lab/am26-1a").get_data(as_text=True),
        )

    def test_delete_lab_without_submissions(self):
        resp = self.post(
            "/admin/lab/am25-1a/delete",
            page="/admin/lab/am25-1a/edit",
            follow_redirects=True,
        )
        self.assertIn("Versuch am25-1a gelöscht.", resp.get_data(as_text=True))
        self.assertNotIn(
            "am25-1a",
            {r["name"] for r in self.conn.execute("SELECT name FROM lab")},
        )

    def test_delete_lab_with_submissions_removes_them_too(self):
        student = self.login(STUDENT, self.app.test_client())
        self.upload(student, {"addone.m": GOOD}, lab="am26-1a")
        page = self.admin.get("/admin/lab/am26-1a/edit").get_data(as_text=True)
        self.assertIn('id="delete-dialog"', page)  # confirmation in the modal
        self.assertIn("Dazu gehört 1 Abgabe", page)
        resp = self.post(
            "/admin/lab/am26-1a/delete",
            page="/admin/lab/am26-1a/edit",
            follow_redirects=True,
        )
        self.assertIn(
            "Versuch am26-1a und 1 Abgabe gelöscht.",
            resp.get_data(as_text=True),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM lab WHERE name = 'am26-1a'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM submission WHERE lab = 'am26-1a'"
            ).fetchone()[0],
            0,
        )

    def test_create_course_then_a_lab_inside_it(self):
        resp = self.post(
            "/admin/course/new",
            {"name": "am28"},
            page="/admin/course/new",
            follow_redirects=True,
        )
        self.assertIn("Kurs am28 angelegt", resp.get_data(as_text=True))
        self.assertTrue(labs.course_exists(self.conn, "am28"))
        index = self.admin.get("/admin/").get_data(as_text=True)
        self.assertIn('id="am28"', index)  # an empty course is shown
        self.assertIn("Dieser Kurs hat noch keine Versuche", index)
        self.assertIn('href="/admin/course/am28/lab/new"', index)
        self.assertEqual(
            self.create(course="am28", ident="2b").status_code, 302
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT course FROM lab WHERE name = 'am28-2b'"
            ).fetchone()[0],
            "am28",
        )

    def test_create_course_validates_the_name(self):
        for bad in ("", "AM28", "am", "am26", "28"):  # "am26" exists already
            with self.subTest(bad=bad):
                resp = self.post(
                    "/admin/course/new",
                    {"name": bad},
                    page="/admin/course/new",
                )
                self.assertEqual(resp.status_code, 400)
        self.assertFalse(labs.course_exists(self.conn, "am"))

    def test_only_empty_courses_can_be_deleted(self):
        labs.create_course(self.conn, "am28")
        resp = self.post("/admin/course/am26/delete", follow_redirects=True)
        self.assertIn("noch Versuche", resp.get_data(as_text=True))
        self.assertTrue(labs.course_exists(self.conn, "am26"))
        index = self.admin.get("/admin/").get_data(as_text=True)
        self.assertIn(
            'id="delete-course-am28"', index
        )  # confirmation dialog only for the empty course
        self.assertNotIn('id="delete-course-am26"', index)
        resp = self.post("/admin/course/am28/delete", follow_redirects=True)
        self.assertIn("Kurs am28 gelöscht", resp.get_data(as_text=True))
        self.assertFalse(labs.course_exists(self.conn, "am28"))
        self.assertEqual(
            self.post("/admin/course/am99/delete").status_code, 404
        )

    def test_clone_course(self):
        resp = self.post(
            "/admin/course/am26/clone",
            {"new_course": "am27", "shift_days": "365"},
            page="/admin/course/am26/clone",
        )
        self.assertEqual(resp.status_code, 302)
        rows = self.conn.execute(
            "SELECT name, active, due_at FROM lab WHERE course = "
            "'am27' ORDER BY name"
        ).fetchall()
        self.assertEqual(
            [(r["name"], r["active"]) for r in rows],
            [("am27-1a", 0), ("am27-1b", 0)],
        )
        self.assertEqual(rows[0]["due_at"], "2100-01-01T22:59:59Z")
        resp = self.post(
            "/admin/course/am26/clone",
            {"new_course": "am27", "shift_days": "0"},
            page="/admin/course/am26/clone",
            follow_redirects=True,
        )
        self.assertIn("gibt es schon", resp.get_data(as_text=True))
        resp = self.post(
            "/admin/course/am26/clone",
            {"new_course": "am28", "shift_days": "viel"},
            page="/admin/course/am26/clone",
            follow_redirects=True,
        )
        self.assertIn("ganze Zahl", resp.get_data(as_text=True))

    # --- Submissions ---
    def submit_and_grade(
        self, email=STUDENT, lab="am26-1a", files=None, required=None
    ):
        if required:
            self.conn.execute(
                "UPDATE lab SET required_files = ? WHERE name = ?",
                (required, lab),
            )
            self.conn.commit()
        client = self.login(email, self.app.test_client())
        location = self.upload(
            client, files or {"addone.m": GOOD}, lab=lab
        ).headers["Location"]
        worker.run_batch(self.conn, self.cfg)
        return client, int(location.rsplit("/", 1)[1])

    def test_submissions_list_download_regrade_delete(self):
        _, sub_id = self.submit_and_grade()
        page = self.admin.get("/admin/lab/am26-1a/submissions").get_data(
            as_text=True
        )
        self.assertIn(STUDENT, page)
        self.assertIn("bestanden", page)

        import zipfile

        resp = self.admin.get(f"/admin/submission/{sub_id}/download")
        self.assertEqual(resp.mimetype, "application/zip")
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        names = zipfile.ZipFile(io.BytesIO(resp.data)).namelist()
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith("/addone.m"))

        self.post(
            f"/admin/submission/{sub_id}/regrade",
            page="/admin/lab/am26-1a/submissions",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM submission WHERE id = ?", (sub_id,)
            ).fetchone()[0],
            "queued",
        )
        worker.run_batch(self.conn, self.cfg)
        self.assertEqual(
            self.conn.execute(
                "SELECT passed FROM submission WHERE id = ?", (sub_id,)
            ).fetchone()[0],
            1,
        )

        self.post(
            f"/admin/submission/{sub_id}/delete",
            page="/admin/lab/am26-1a/submissions",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM submission").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.admin.get(f"/admin/submission/{sub_id}/download").status_code,
            404,
        )

    def test_last_submissions_download_no_longer_exists(self):
        self.assertEqual(
            self.admin.get("/admin/lab/am26-1a/download").status_code, 404
        )
        self.assertNotIn(
            "Letzte Abgaben",
            self.admin.get("/admin/lab/am26-1a/submissions").get_data(
                as_text=True
            ),
        )

    def test_unknown_submission_is_404(self):
        self.assertEqual(
            self.admin.get("/admin/submission/999/download").status_code, 404
        )
        self.assertEqual(
            self.post("/admin/submission/999/regrade").status_code, 404
        )

    # --- Viewing source code ---
    def test_admin_and_student_see_the_highlighted_source(self):
        src = b"function y = addone(x) % <b>fett</b>\n  y = x + 1;\nend\n"
        client, sub_id = self.submit_and_grade(files={"addone.m": src})
        page = self.admin.get(f"/submission/{sub_id}").get_data(as_text=True)
        self.assertIn("Abgegebene Dateien", page)
        self.assertIn('<span class="tok-keyword">function</span>', page)
        self.assertIn(
            '<span class="tok-comment">% &lt;b&gt;fett&lt;/b&gt;</span>', page
        )  # HTML is escaped
        self.assertNotIn("<b>fett</b>", page)
        self.assertIn("3 Zeilen", page)
        own = client.get(f"/submission/{sub_id}").get_data(as_text=True)
        self.assertIn("Abgegebene Dateien", own)  # the student sees it too
        self.assertIn('<span class="tok-keyword">function</span>', own)
        self.assertNotIn("<b>fett</b>", own)
        for admin_only in (
            "Testbench des Versuchs",
            "Ausgabe der Prüfung",
            "Alle Dateien (ZIP)",
        ):
            self.assertNotIn(admin_only, own)

    def test_source_view_handles_binary_files(self):
        _, sub_id = self.submit_and_grade(
            files={"addone.m": GOOD, "plot.png": b"\x89PNG\x00\x01"},
            required="addone.m\nplot.png\n",
        )
        page = self.admin.get(f"/submission/{sub_id}").get_data(as_text=True)
        self.assertIn("plot.png", page)
        self.assertIn("Binärdatei", page)

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

    def test_images_of_a_submission_are_shown_to_admins_and_the_owner(self):
        client, sub_id = self.submit_and_grade(
            files={"addone.m": GOOD, "plot.png": self.PNG},
            required="addone.m\nplot.png\n",
        )
        page = self.admin.get(f"/submission/{sub_id}").get_data(as_text=True)
        self.assertIn(
            f'<img src="/admin/submission/{sub_id}/image/plot.png"', page
        )
        self.assertNotIn("Binärdatei", page)
        resp = self.admin.get(f"/admin/submission/{sub_id}/image/plot.png")
        self.assertEqual(
            (resp.status_code, resp.mimetype, resp.data),
            (200, "image/png", self.PNG),
        )
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", resp.headers["Cache-Control"])
        own = client.get(f"/submission/{sub_id}").get_data(as_text=True)
        self.assertIn(f'<img src="/submission/{sub_id}/image/plot.png"', own)
        self.assertNotIn("/admin/", own.split("<main")[1])
        mine = client.get(f"/submission/{sub_id}/image/plot.png")
        self.assertEqual(
            (mine.status_code, mine.mimetype, mine.data),
            (200, "image/png", self.PNG),
        )
        other = self.login(OTHER, self.app.test_client())
        self.assertEqual(
            other.get(f"/submission/{sub_id}/image/plot.png").status_code, 404
        )
        self.assertEqual(
            self.admin.get(f"/submission/{sub_id}/image/plot.png").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/submission/{sub_id}/image/plot.png"
            ).status_code,
            302,
        )

    def test_only_admins_get_images_and_only_real_images(self):
        client, sub_id = self.submit_and_grade(
            files={"addone.m": GOOD, "plot.png": self.PNG},
            required="addone.m\nplot.png\n",
        )
        self.assertEqual(
            client.get(
                f"/admin/submission/{sub_id}/image/plot.png"
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                f"/admin/submission/{sub_id}/image/plot.png"
            ).status_code,
            302,
        )
        for path in (
            f"/admin/submission/{sub_id}/image/addone.m",  # not an image
            f"/admin/submission/{sub_id}/image/missing.png",  # no such file
            # no path tricks
            f"/admin/submission/{sub_id}/image/..%2Fplot.png",
            "/admin/submission/999/image/plot.png",
        ):  # no such submission
            with self.subTest(path=path):
                self.assertEqual(self.admin.get(path).status_code, 404)

    def test_a_text_file_with_an_image_name_is_not_served_as_an_image(self):
        _, sub_id = self.submit_and_grade(
            files={
                "addone.m": GOOD,
                "plot.png": b"<html><script>alert(1)</script>",
            },
            required="addone.m\nplot.png\n",
        )
        self.assertEqual(
            self.admin.get(
                f"/admin/submission/{sub_id}/image/plot.png"
            ).status_code,
            404,
        )
        self.assertNotIn(
            "/image/",
            self.admin.get(f"/submission/{sub_id}").get_data(as_text=True),
        )

    # --- Attempt limit and counter ---
    def test_limit_refers_to_supervisors_and_delete_gives_one_back(
        self,
    ):
        self.cfg["limits"]["max_attempts"] = 1
        client, sub_id = self.submit_and_grade()
        page = client.get("/lab/am26-1a").get_data(as_text=True)
        self.assertIn("Maximalzahl von 1 Abgaben", page)
        self.assertIn("wenden Sie sich an Ihre Betreuer", page)
        self.assertNotIn('type="file"', page)
        self.assertIn("1 / 1", client.get("/").get_data(as_text=True))
        resp = self.upload(
            client, {"addone.m": GOOD}, lab="am26-1a"
        )  # a direct POST is rejected as well
        self.assertEqual(resp.status_code, 302)
        self.assertIn(
            "Betreuer", client.get("/lab/am26-1a").get_data(as_text=True)
        )

        admin_page = self.admin.get("/admin/lab/am26-1a/submissions").get_data(
            as_text=True
        )
        self.assertIn("1 von 1", admin_page)
        self.assertIn("Grenze erreicht", admin_page)
        self.assertIn("ist wieder eine Abgabe möglich", admin_page)
        self.assertIn("Der Student hat danach eine Abgabe mehr", admin_page)

        self.post(
            f"/admin/submission/{sub_id}/delete",
            page="/admin/lab/am26-1a/submissions",
        )
        page = client.get("/lab/am26-1a").get_data(as_text=True)
        self.assertIn("0 von höchstens 1", page)
        self.assertIn('type="file"', page)
        self.assertNotIn(
            "Grenze erreicht",
            self.admin.get("/admin/lab/am26-1a/submissions").get_data(
                as_text=True
            ),
        )
        self.assertEqual(
            self.upload(client, {"addone.m": GOOD}, lab="am26-1a").status_code,
            302,
        )

    def test_no_route_for_extra_attempts(self):
        self.assertEqual(
            self.post(
                "/admin/lab/am26-1a/grant", {"email": OTHER, "extra": "3"}
            ).status_code,
            404,
        )

    # --- Retention, deletion warning, archive ---
    def age_submissions(self, days_until_deletion, lab="am26-1a"):
        """Set deadline and submissions so that the deletion is due in
        `days_until_deletion` days."""
        from datetime import timedelta

        from emptydiff.core import util

        base = util.utcnow() - timedelta(
            days=self.cfg["retention_days"] - days_until_deletion
        )
        self.conn.execute(
            "UPDATE lab SET due_at = ? WHERE name = ?",
            (util.to_iso(base), lab),
        )
        self.conn.execute(
            "UPDATE submission SET created_at = ? WHERE lab = ?",
            (util.to_iso(base), lab),
        )
        self.conn.commit()

    def test_no_warning_when_deletion_is_far_away(self):
        self.submit_and_grade()
        self.assertNotIn(
            "unwiderruflich gelöscht",
            self.admin.get("/admin/").get_data(as_text=True),
        )
        self.assertNotRegex(
            self.admin.get("/admin/").get_data(as_text=True), r"⚠ Löschung \d"
        )  # no deletion badge with a date

    def test_warning_on_the_overview_only(self):
        self.submit_and_grade()
        self.age_submissions(120)
        index = self.admin.get("/admin/").get_data(as_text=True)
        self.assertIn(
            "in den nächsten 9 Monaten unwiderruflich gelöscht", index
        )
        self.assertIn('href="/admin/lab/am26-1a/archive"', index)
        self.assertIn("⚠ Löschung", index)
        self.assertNotIn("retention-warning urgent", index)
        for path in (
            "/admin/lab/am26-1a/submissions",
            "/admin/lab/am26-1a/edit",
        ):  # no repetition on subpages
            self.assertNotIn(
                "retention-warning",
                self.admin.get(path).get_data(as_text=True),
            )

    def test_urgent_warning_within_30_days(self):
        self.submit_and_grade()
        self.age_submissions(12)
        self.assertIn(
            "retention-warning urgent",
            self.admin.get("/admin/").get_data(as_text=True),
        )

    def test_students_never_see_retention_warnings(self):
        client, _ = self.submit_and_grade()
        self.age_submissions(12)
        for path in ("/", "/lab/am26-1a"):
            self.assertNotIn(
                "unwiderruflich", client.get(path).get_data(as_text=True)
            )

    def test_privacy_config_warning_for_admins(self):
        self.assertIn(
            "Die Datenschutzerklärung ist unvollständig",
            self.admin.get("/admin/").get_data(as_text=True),
        )
        self.cfg["privacy"].update(
            controller="X",
            controller_address="Y",
            controller_email="z@example.org",
            authority="A",
        )
        from emptydiff.web import create_app

        admin = self.login(ADMIN, create_app(self.cfg).test_client())
        self.assertNotIn(
            "unvollständig", admin.get("/admin/").get_data(as_text=True)
        )

    def test_archive_download(self):
        import zipfile

        _, sub_id = self.submit_and_grade()
        resp = self.admin.get("/admin/lab/am26-1a/archive")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(
            "am26-1a-archive.zip", resp.headers["Content-Disposition"]
        )
        self.assertEqual(int(resp.headers["Content-Length"]), len(resp.data))
        names = zipfile.ZipFile(io.BytesIO(resp.data)).namelist()
        self.assertIn("am26-1a/overview.csv", names)
        self.assertIn("am26-1a/testbench.m", names)
        self.assertTrue(any(n.endswith("/001/addone.m") for n in names))
        self.assertTrue(any(n.startswith("am26-1a/reports/") for n in names))

    def test_archive_is_admin_only_and_404_for_unknown_labs(self):
        student = self.login(OTHER, self.app.test_client())
        self.assertEqual(
            student.get("/admin/lab/am26-1a/archive").status_code, 403
        )
        self.assertEqual(
            self.app.test_client()
            .get("/admin/lab/am26-1a/archive")
            .status_code,
            302,
        )
        self.assertEqual(
            self.admin.get("/admin/lab/am99-1a/archive").status_code, 404
        )

    # --- Credit list ---
    def test_credit_matrix_and_csv(self):
        self.submit_and_grade(STUDENT)
        self.submit_and_grade(
            OTHER,
            files={"addone.m": b"function y = addone(x)\n y = 0;\nend\n"},
        )
        page = self.admin.get("/admin/credit?course=am26").get_data(
            as_text=True
        )
        self.assertIn(STUDENT, page)
        self.assertIn('class="cell pass"', page)
        self.assertIn('class="cell fail"', page)
        csv_resp = self.admin.get("/admin/credit.csv?course=am26")
        self.assertEqual(csv_resp.mimetype, "text/csv")
        self.assertIn("attachment", csv_resp.headers["Content-Disposition"])
        lines = csv_resp.get_data(as_text=True).strip().splitlines()
        self.assertEqual(lines[0], "email,am26-1a,am26-1b,bestanden")
        self.assertEqual(
            sorted(lines[1:]),
            sorted([f"{STUDENT},bestanden,,1", f"{OTHER},nicht bestanden,,0"]),
        )

    def test_credit_defaults_to_newest_course_and_rejects_unknown(self):
        self.assertIn(
            'aria-current="page">am26',
            self.admin.get("/admin/credit").get_data(as_text=True),
        )
        self.assertEqual(
            self.admin.get("/admin/credit?course=am99").status_code, 404
        )
        self.assertEqual(
            self.admin.get("/admin/credit.csv?course=../x").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()


class HelpTextsTest(WebTestCase):
    """Hints that explain the pages to students and admins."""

    def test_login_explains_domains_case_and_code_lifetime(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn("@student.example.edu, @example.edu", page)
        self.assertIn("Groß- und Kleinschreibung spielt keine Rolle", page)
        self.client.post(
            "/login",
            data={"email": STUDENT, "csrf_token": self.token(self.client)},
        )
        verify = self.client.get("/login/verify").get_data(as_text=True)
        self.assertIn("15 Minuten gültig", verify)
        self.assertIn("Spam-Ordner", verify)

    def test_contact_address_is_shown_only_when_configured(self):
        add_lab(self.conn)
        client = self.login(STUDENT)
        self.assertNotIn("mailto:", client.get("/").get_data(as_text=True))
        from emptydiff.web import create_app

        self.cfg["privacy"]["contact_email"] = "betreuer@example.org"
        self.cfg["limits"]["max_attempts"] = 1
        app = create_app(self.cfg)
        client = self.login(STUDENT, app.test_client())
        self.assertIn(
            'href="mailto:betreuer@example.org">betreuer@example.org</a>',
            client.get("/").get_data(as_text=True),
        )
        self.upload(client, {"addone.m": GOOD})
        worker.run_batch(self.conn, self.cfg)
        page = client.get("/lab/am24-1a").get_data(as_text=True)
        self.assertIn("Maximalzahl von 1 Abgaben", page)
        self.assertIn('href="mailto:betreuer@example.org"', page)
        resp = self.upload(
            client, {"addone.m": GOOD}
        )  # plain text variant of the same hint in the error message
        self.assertIn(
            "(betreuer@example.org)",
            client.get(resp.headers["Location"]).get_data(as_text=True),
        )

    def test_student_pages_explain_states_files_and_next_steps(self):
        add_lab(self.conn)
        add_lab(self.conn, "am24-1b", due=PAST)
        client = self.login(STUDENT)
        index = client.get("/").get_data(as_text=True)
        self.assertIn(
            "zeigt das Ergebnis der automatischen Tests Ihrer letzten Abgabe",
            index,
        )  # status legend
        self.assertIn("So funktioniert’s", index)
        lab_page = client.get("/lab/am24-1a").get_data(as_text=True)
        self.assertIn("Geforderte Dateien", lab_page)
        self.assertIn(
            "Fehlt eine, gilt die Abgabe als unvollständig", lab_page
        )
        self.assertIn("Insgesamt höchstens 5 MB", lab_page)
        self.assertIn("So funktioniert’s", lab_page)
        self.assertNotIn("Die Frist ist abgelaufen", lab_page)
        self.assertIn(
            "Die Frist ist abgelaufen",
            client.get("/lab/am24-1b").get_data(as_text=True),
        )
        failing = b"function r = addone(x)\n  r = x;\nend\n"
        location = self.upload(client, {"addone.m": failing}).headers[
            "Location"
        ]
        pending = client.get(location).get_data(as_text=True)
        self.assertIn("meist nur wenige Sekunden", pending)
        worker.run_batch(self.conn, self.cfg)
        failed = client.get(location).get_data(as_text=True)
        self.assertIn("Mindestens ein Test ist fehlgeschlagen", failed)
        self.assertIn("Korrigierte Dateien abgeben", failed)
        good = self.upload(client, {"addone.m": GOOD}).headers["Location"]
        worker.run_batch(self.conn, self.cfg)
        passed = client.get(good).get_data(as_text=True)
        self.assertIn("Alle automatischen Tests sind durchgelaufen", passed)
        self.assertNotIn("Korrigierte Dateien abgeben", passed)

    def test_admin_help_page_and_nav(self):
        admin = self.login(ADMIN, self.app.test_client())
        page = admin.get("/admin/help").get_data(as_text=True)
        self.assertIn("Ablauf in Schritten", page)
        self.assertIn(
            "730 Tage (rund 2.0 Jahre)", page
        )  # numbers come from the configuration
        self.assertIn("Höchstens 7 Abgaben", page)
        self.assertIn(
            'href="/admin/help"', admin.get("/admin/").get_data(as_text=True)
        )
        self.assertIn(
            "Zahl der Studierenden, die in diesem Versuch abgegeben haben",
            admin.get("/admin/").get_data(as_text=True),
        )
        student = self.login(STUDENT, self.app.test_client())
        self.assertEqual(student.get("/admin/help").status_code, 403)
        self.assertEqual(
            self.app.test_client().get("/admin/help").status_code, 302
        )
