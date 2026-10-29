# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import glob
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from helpers import ADMIN, GOOD, STUDENT, DbTestCase, add_lab
from emptydiff.core import config, credit, i18n, mail, report

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# keys that the code assembles from parts (the literal is not complete
# there)
DYNAMIC_PREFIXES = (
    "http.",
    "credit.state.",
    "ln.",
)  # ln.<language>.*: the key is built from the language of a lab


def sources():
    files = glob.glob(
        os.path.join(ROOT, "emptydiff", "**", "*.py"), recursive=True
    )
    files += glob.glob(os.path.join(ROOT, "emptydiff", "templates", "*.html"))
    return {f: open(f, encoding="utf-8").read() for f in files}


class StringsFileTest(unittest.TestCase):
    def test_files_are_merged_and_a_key_in_two_files_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:

            def write(name, blocks):
                with open(
                    os.path.join(directory, name), "w", encoding="utf-8"
                ) as f:
                    json.dump(blocks, f)

            write("a.json", {"DE": {"x.one": "eins"}, "EN": {"x.one": "one"}})
            write("b.json", {"DE": {"x.two": "zwei"}, "EN": {"x.two": "two"}})
            write(
                "fr.json", {"FR": {"x.one": "un"}}
            )  # a language in a file of its own
            with (
                mock.patch.object(i18n, "DIRECTORY", directory),
                mock.patch.object(i18n, "_cache", None),
            ):
                self.assertEqual(
                    i18n.strings(),
                    {
                        "DE": {"x.one": "eins", "x.two": "zwei"},
                        "EN": {"x.one": "one", "x.two": "two"},
                        "FR": {"x.one": "un"},
                    },
                )
                write("c.json", {"DE": {"x.one": "doppelt"}})
                with mock.patch.object(i18n, "_cache", None):
                    with self.assertRaises(ValueError) as ctx:
                        i18n.strings()
                    self.assertIn("DE:x.one", str(ctx.exception))

    def test_every_file_has_the_same_keys_in_every_language(self):
        for path in sorted(glob.glob(os.path.join(i18n.DIRECTORY, "*.json"))):
            with open(path, encoding="utf-8") as f:
                blocks = json.load(f)
            with self.subTest(file=os.path.basename(path)):
                self.assertEqual(set(blocks["DE"]), set(blocks["EN"]))

    def test_every_language_has_the_same_keys(self):
        data = i18n.strings()
        self.assertEqual(set(data["DE"]), set(data["EN"]))
        for lang in data:
            with self.subTest(lang=lang):
                self.assertIn("lang.name", data[lang])
                self.assertEqual(
                    set(data[lang]) - set(data["EN"]), set()
                )  # nothing that English does not know

    def test_placeholders_match_between_languages(self):
        data = i18n.strings()
        for key, de in data["DE"].items():
            with self.subTest(key=key):
                self.assertEqual(
                    set(PLACEHOLDER.findall(de)),
                    set(PLACEHOLDER.findall(data["EN"][key])),
                )

    def test_plural_keys_come_in_pairs(self):
        keys = set(i18n.strings()["DE"])
        for key in keys:
            if key.endswith("_one"):
                self.assertIn(key[:-4] + "_other", keys)
            if key.endswith("_other"):
                self.assertIn(key[:-6] + "_one", keys)

    def test_texts_are_not_empty_except_where_intended(self):
        for lang, texts in i18n.strings().items():
            for key, text in texts.items():
                if key != "credit.state.none":
                    self.assertTrue(text.strip(), f"{lang}:{key}")

    def test_every_key_used_in_code_exists(self):
        keys = set(i18n.strings()["EN"])
        pattern = re.compile(
            r"""(?:\bt|\btn|\bplural|translate\([^,()]+,|\bError)"""
            r"""\(\s*["']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["']"""
        )
        for path, text in sources().items():
            for key in pattern.findall(text):
                with self.subTest(file=os.path.basename(path), key=key):
                    if key in keys:
                        continue
                    self.assertTrue(
                        {key + "_one", key + "_other"} <= keys,
                        f"{key} fehlt in strings/",
                    )

    def test_no_unused_keys(self):
        text = "\n".join(sources().values())
        for key in i18n.strings()["EN"]:
            base = re.sub(r"_(one|other)$", "", key)
            if key.startswith(DYNAMIC_PREFIXES):
                continue
            with self.subTest(key=key):
                self.assertTrue(
                    f"'{base}'" in text or f'"{base}"' in text,
                    f"{key} wird nirgends benutzt",
                )


class LookupTest(unittest.TestCase):
    def test_translate_and_fallback_chain(self):
        data = {
            "DE": {
                "a": "Hallo {n}",
                "only_de": "nur deutsch",
                "lang.name": "Deutsch",
            },
            "EN": {"a": "Hello {n}", "lang.name": "English"},
            "FR": {"lang.name": "Français"},
        }
        with mock.patch.object(i18n, "_cache", data):
            self.assertEqual(i18n.translate("DE", "a", n=1), "Hallo 1")
            self.assertEqual(
                i18n.translate("FR", "a", n=2), "Hello 2"
            )  # missing in FR: English
            self.assertEqual(
                i18n.translate("FR", "only_de"), "nur deutsch"
            )  # missing in FR and EN: German
            self.assertEqual(
                i18n.translate("FR", "unbekannt"), "unbekannt"
            )  # finally the key
            self.assertEqual(i18n.languages(), ["DE", "EN", "FR"])
            self.assertEqual(i18n.language_name("FR"), "Français")

    def test_plural(self):
        data = {
            "DE": {"x_one": "{n} Datei", "x_other": "{n} Dateien"},
            "EN": {"x_one": "{n} file", "x_other": "{n} files"},
        }
        with mock.patch.object(i18n, "_cache", data):
            self.assertEqual(i18n.plural("EN", "x", 1), "1 file")
            self.assertEqual(i18n.plural("EN", "x", 0), "0 files")
            self.assertEqual(i18n.plural("DE", "x", 5), "5 Dateien")

    def test_best_language(self):
        best = i18n.best_language
        self.assertEqual(best([("de-DE", 1.0), ("en", 0.5)], "EN"), "DE")
        self.assertEqual(best([("en-US", 1.0), ("de", 0.9)], "DE"), "EN")
        self.assertEqual(
            best([("fr", 1.0), ("de", 0.5)], "EN"), "DE"
        )  # the first available by weight
        self.assertEqual(
            best([("fr", 1.0), ("es", 0.9)], "EN"), "EN"
        )  # nothing available: default
        self.assertEqual(best([("fr", 1.0)], "DE"), "DE")
        self.assertEqual(best([], "EN"), "EN")
        self.assertEqual(best([("DE-at", 1.0)], "EN"), "DE")
        self.assertEqual(best([("*", 1.0)], "EN"), "EN")
        self.assertEqual(
            best([("fr", 1.0)], "XX"), "EN"
        )  # unknown default: English

    def test_localized_error_message_per_language(self):
        err = i18n.LocalizedError("err.lab.exists", name="am26-1a")
        self.assertEqual(
            err.message("DE"), "Den Versuch am26-1a gibt es schon."
        )
        self.assertEqual(err.message("EN"), "The lab am26-1a already exists.")
        self.assertEqual(
            str(err), err.message("EN")
        )  # logs and the command line are English


class CoreTextsTest(DbTestCase):
    def test_report_and_mail_follow_the_language(self):
        sub = dict(
            status="done",
            timed_out=1,
            truncated=0,
            finished=0,
            checks="[]",
            log="",
            error_log="",
        )
        self.assertIn("Endlosschleifen", report.build(sub, "DE").notes[0])
        self.assertIn("infinite loops", report.build(sub, "EN").notes[0])
        sub["timed_out"] = 0
        self.assertIn("nicht vollständig", report.build(sub, "DE").notes[0])
        self.assertIn(
            "not be run completely", report.build(sub, "EN").notes[0]
        )
        subject, body = mail.login_code_mail("ABCD1234", 15, "EN")
        self.assertEqual(subject, "EmptyDiff: Your login code")
        self.assertIn("valid for 15 minutes", body)
        self.assertEqual(
            mail.login_code_mail("ABCD1234", 15, "DE")[0],
            "EmptyDiff: Ihr Anmeldecode",
        )
        self.assertEqual(
            mail.login_code_mail("ABCD1234", 15)[0],
            "EmptyDiff: Your login code",
        )  # default: operator language

    def test_unknown_default_language_is_rejected(self):
        self.cfg["ui"]["default_language"] = "XX"
        with self.assertRaises(config.ConfigError):
            config._validate(self.cfg)

    def test_csv_language(self):
        add_lab(self.conn)
        from emptydiff.core import submissions

        submissions.submit(
            self.conn, self.cfg, "am24-1a", STUDENT, {"addone.m": GOOD}
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = 1, late = 1"
        )
        self.conn.commit()
        text = credit.to_csv(*credit.matrix(self.conn, "am24"), lang="EN")
        self.assertEqual(
            text.strip().splitlines(),
            ["email,am24-1a,passed", f"{STUDENT},passed (late),1"],
        )


@unittest.skipIf(
    flask is None,
    "Flask nicht installiert (Tests im dev/-Container ausführen)",
)
class LanguageWebTest(DbTestCase):
    def setUp(self):
        super().setUp()
        from emptydiff.web import create_app

        self.app = create_app(self.cfg)  # default language of the tests: DE
        self.client = self.app.test_client()

    def page(self, path="/login", **headers):
        return self.client.get(path, headers=headers).get_data(as_text=True)

    def test_browser_language_decides(self):
        self.assertIn(
            "Code anfordern",
            self.page(**{"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}),
        )
        self.assertIn(
            "Request code", self.page(**{"Accept-Language": "en-US,en;q=0.9"})
        )
        self.assertIn(
            "Request code",
            self.page(**{"Accept-Language": "fr;q=1.0, en;q=0.9, de;q=0.5"}),
        )
        self.assertIn(
            '<html lang="en">', self.page(**{"Accept-Language": "en"})
        )
        self.assertIn(
            '<html lang="de">', self.page(**{"Accept-Language": "de"})
        )

    def test_default_language_when_the_browser_language_is_unavailable(self):
        self.assertIn(
            "Code anfordern", self.page(**{"Accept-Language": "fr, es"})
        )  # default of this system: DE
        self.assertIn(
            "Code anfordern", self.page()
        )  # also without a statement
        from emptydiff.web import create_app

        self.cfg["ui"]["default_language"] = "EN"
        english = create_app(self.cfg).test_client()
        self.assertIn(
            "Request code",
            english.get("/login", headers={"Accept-Language": "fr"}).get_data(
                as_text=True
            ),
        )
        self.assertIn(
            "Request code", english.get("/login").get_data(as_text=True)
        )
        self.assertIn(
            "Code anfordern",
            english.get("/login", headers={"Accept-Language": "de"}).get_data(
                as_text=True
            ),
        )

    def test_footer_offers_every_language_and_marks_the_current_one(self):
        page = self.page("/login", **{"Accept-Language": "en"})
        self.assertIn(
            'href="/lang/de?next=/login" lang="de" hreflang="de">Deutsch</a>',
            page,
        )
        self.assertIn(
            'href="/lang/en?next=/login" lang="en" '
            'hreflang="en" aria-current="true">English</a>',
            page,
        )

    def test_switching_is_remembered_in_the_session_and_overrides_the_browser(
        self,
    ):
        resp = self.client.get(
            "/lang/en?next=/privacy", headers={"Accept-Language": "de"}
        )
        self.assertEqual(
            (resp.status_code, resp.headers["Location"]), (302, "/privacy")
        )
        self.assertIn(
            "Privacy statement",
            self.page("/privacy", **{"Accept-Language": "de"}),
        )
        self.client.get("/lang/de")
        self.assertIn(
            "Datenschutzerklärung",
            self.page("/privacy", **{"Accept-Language": "en"}),
        )

    def test_switch_validates_input(self):
        self.assertEqual(self.client.get("/lang/xx").status_code, 404)
        for bad in (
            "//evil.example",
            "https://evil.example",
            "/\\evil.example",
            "evil",
            "",
        ):
            with self.subTest(next=bad):
                self.assertEqual(
                    self.client.get(
                        "/lang/en", query_string={"next": bad}
                    ).headers["Location"],
                    "/",
                )
        self.assertEqual(
            self.client.get("/lang/EN").status_code, 302
        )  # case does not matter

    def test_choice_survives_login_and_logout(self):
        from helpers import STUDENT
        import re

        self.client.get("/lang/en")
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', self.page("/login")
        ).group(1)
        self.client.post(
            "/login", data={"email": STUDENT, "csrf_token": token}
        )
        body = next(
            m["body"] for m in mail.SIMULATED if m["recipient"] == STUDENT
        )
        self.assertIn(
            "Your login code for EmptyDiff", body
        )  # the mail comes in the chosen language
        code = re.search(r"^    ([0-9A-Z]{8})$", body, re.M).group(1)
        token = re.search(
            r'name="csrf_token" value="([^"]+)"', self.page("/login/verify")
        ).group(1)
        self.client.post(
            "/login/verify", data={"code": code, "csrf_token": token}
        )
        page = self.page("/")
        self.assertIn("Lab assignments", page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.client.post("/logout", data={"csrf_token": token})
        self.assertIn("Log in", self.page("/login"))

    def test_error_pages_flash_messages_and_dates_are_translated(self):
        self.assertIn(
            "This page does not exist.",
            self.page("/nope", **{"Accept-Language": "en"}),
        )
        self.assertIn(
            "Diese Seite gibt es nicht.",
            self.page("/nope", **{"Accept-Language": "de"}),
        )
        self.assertEqual(self.client.get("/nope").status_code, 404)

    def test_every_page_renders_in_every_language(self):
        """Smoke test: all pages in all languages, without falling back
        to raw keys."""
        import re

        add_lab(self.conn, "am26-1a")
        from emptydiff.core import labs

        labs.create_course(
            self.conn, "am30"
        )  # an empty course (delete dialog)
        from helpers import OTHER
        from emptydiff.core import submissions

        sid = submissions.submit(
            self.conn, self.cfg, "am26-1a", OTHER, {"addone.m": GOOD}
        )
        self.conn.execute(
            "UPDATE submission SET status = 'done', passed = 1 WHERE id = ?",
            (sid,),
        )
        self.conn.commit()
        paths = [
            "/",
            "/lab/am26-1a",
            f"/submission/{sid}",
            "/privacy",
            "/admin/",
            "/admin/course/new",
            "/admin/course/am26/lab/new",
            "/admin/lab/am26-1a/edit",
            "/admin/lab/am26-1a/submissions",
            "/admin/course/am26/clone",
            "/admin/credit",
            "/admin/backup",
        ]
        for lang in i18n.languages():
            admin = self._login(ADMIN)
            admin.get(f"/lang/{lang.lower()}")
            for path in paths:
                with self.subTest(lang=lang, path=path):
                    resp = admin.get(path)
                    self.assertEqual(resp.status_code, 200)
                    text = resp.get_data(as_text=True)
                    self.assertNotRegex(
                        text,
                        r"\b(?:adm|form|lab|sub|bk|bkc|priv|index|login|verify"
                        r"|credit|clone|err|flash|nav|footer|status)"
                        r"\.[a-z_.]+_?\w*\b(?=[<\s\"'])",
                    )
                    self.assertIn(f'<html lang="{lang.lower()}">', text)

    def _login(self, email):
        import re

        client = self.app.test_client()
        token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            client.get("/login").get_data(as_text=True),
        ).group(1)
        client.post("/login", data={"email": email, "csrf_token": token})
        body = next(
            m["body"]
            for m in reversed(mail.SIMULATED)
            if m["recipient"] == email
        )
        code = re.search(r"^    ([0-9A-Z]{8})$", body, re.M).group(1)
        token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            client.get("/login/verify").get_data(as_text=True),
        ).group(1)
        client.post("/login/verify", data={"code": code, "csrf_token": token})
        return client

    def test_dates_use_the_language_format(self):
        add_lab(self.conn, "am26-1a", due="2026-11-10T22:59:59Z")
        user = self._login(STUDENT)
        self.assertIn("10.11.2026 23:59", user.get("/").get_data(as_text=True))
        user.get("/lang/en")
        self.assertIn("2026-11-10 23:59", user.get("/").get_data(as_text=True))

    def test_a_new_language_needs_only_strings_files(self):
        data = {lang: dict(texts) for lang, texts in i18n.strings().items()}
        data["FR"] = {"lang.name": "Français", "login.title": "Connexion"}
        with mock.patch.object(i18n, "_cache", data):
            page = self.page("/login", **{"Accept-Language": "fr-CH"})
            self.assertIn("Connexion", page)  # translated
            self.assertIn("Request code", page)  # missing: English
            self.assertIn("Français</a>", page)  # in the switcher
            self.assertIn('<html lang="fr">', page)


if __name__ == "__main__":
    unittest.main()
