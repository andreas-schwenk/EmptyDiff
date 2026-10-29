# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

from emptydiff.core import highlight, languages


def kinds(text):
    return [(k, t) for k, t in languages.get("octave").tokens(text) if k]


class TokensTest(unittest.TestCase):
    def test_roundtrip_keeps_text(self):
        src = (
            "function y = f(x) % c\n  s = 'a''b'; t "
            "= x';\n%{\nblock\n%}\nend\n"
        )
        self.assertEqual(
            "".join(t for _, t in languages.get("octave").tokens(src)), src
        )

    def test_keywords_builtins_numbers_comments(self):
        self.assertEqual(
            kinds("for i = 1:10 % Schleife\n  disp(i)\nend"),
            [
                ("keyword", "for"),
                ("number", "1"),
                ("number", "10"),
                ("comment", "% Schleife"),
                ("builtin", "disp"),
                ("keyword", "end"),
            ],
        )

    def test_number_forms(self):
        self.assertEqual(
            [t for k, t in kinds("1.5e-3 + .5 + 2i + 0x1F + 7.")],
            ["1.5e-3", ".5", "2i", "0x1F", "7."],
        )

    def test_strings_and_transpose(self):
        self.assertEqual(
            kinds("s = 'a''b'; t = x'; u = a.';"), [("string", "'a''b'")]
        )
        self.assertEqual(kinds('q = "x\\"y";'), [("string", '"x\\"y"')])
        self.assertEqual(
            kinds("v = [1 'z'];"), [("number", "1"), ("string", "'z'")]
        )

    def test_comment_markers_inside_strings_are_text(self):
        self.assertEqual(
            kinds("disp('100 % sicher')"),
            [("builtin", "disp"), ("string", "'100 % sicher'")],
        )

    def test_hash_comment_continuation_and_block_comment(self):
        self.assertEqual(
            kinds("x = 1 # c\ny = x + ...\n z\n%{\nnot code end\n%}\nw"),
            [
                ("number", "1"),
                ("comment", "# c"),
                ("comment", "..."),
                ("comment", "%{"),
                ("comment", "not code end"),
                ("comment", "%}"),
            ],
        )

    def test_unterminated_quote_is_not_a_string(self):
        self.assertEqual(kinds("x = 'abc"), [])

    def test_html_is_not_interpreted(self):
        tokens = languages.get("octave").tokens(
            "disp('<script>alert(1)</script>')"
        )
        self.assertIn(("string", "'<script>alert(1)</script>'"), tokens)


class ViewTest(unittest.TestCase):
    def test_m_files_are_highlighted_others_are_plain(self):
        v = highlight.view("a.m", b"x = 1;\r\ny = 2;\r\n\r\n", "octave")
        self.assertEqual((v["kind"], v["lines"]), ("text", 2))
        self.assertTrue(any(k == "number" for k, _ in v["tokens"]))
        self.assertEqual(
            highlight.view("a.txt", b"x = 1;", "octave")["tokens"],
            [(None, "x = 1;")],
        )
        self.assertEqual(
            highlight.view("a.m", b"x = 1;")["tokens"], [(None, "x = 1;")]
        )  # no language: plain
        self.assertEqual(
            highlight.view("a.m", b"x = 1;", "nonsense")["tokens"],
            [(None, "x = 1;")],
        )

    def test_encodings(self):
        self.assertEqual(
            highlight.view("a.m", "% Größe".encode("utf-8"), "octave")[
                "tokens"
            ][0][1],
            "% Größe",
        )
        self.assertEqual(
            highlight.view("a.m", "% Größe".encode("cp1252"), "octave")[
                "tokens"
            ][0][1],
            "% Größe",
        )

    def test_binary_and_large(self):
        self.assertEqual(
            highlight.view("a.png", b"\x89PNG\x00\x00")["kind"], "binary"
        )  # not a real PNG

    def test_images_are_recognized_by_name_and_content(self):
        samples = {
            "a.png": (b"\x89PNG\r\n\x1a\n" + b"x" * 20, "image/png"),
            "A.JPG": (b"\xff\xd8\xff\xe0" + b"x" * 20, "image/jpeg"),
            "b.jpeg": (b"\xff\xd8\xff\xdb", "image/jpeg"),
            "c.gif": (b"GIF89a" + b"x" * 10, "image/gif"),
            "d.bmp": (b"BM" + b"x" * 30, "image/bmp"),
            "e.webp": (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        }
        for name, (content, mime) in samples.items():
            with self.subTest(name=name):
                view = highlight.view(name, content)
                self.assertEqual((view["kind"], view["mime"]), ("image", mime))

    def test_an_image_name_without_image_content_is_not_an_image(self):
        for name, content in (
            ("a.png", b"function y = f(x)\nend\n"),
            ("a.jpg", b"GIF"),
            ("a.svg", b"<svg onload='x'/>"),
            ("a.txt", b"\x89PNG\r\n\x1a\n"),
            ("png", b"\x89PNG\r\n\x1a\n"),
            ("a.bmp", b"RIFF\x00\x00\x00\x00WAVE"),
        ):
            with self.subTest(name=name):
                self.assertIsNone(highlight.image_type(name, content))
                self.assertNotEqual(
                    highlight.view(name, content)["kind"], "image"
                )

    def test_a_mislabeled_real_image_is_shown_with_its_real_type(self):
        self.assertEqual(
            highlight.image_type("foto.jpg", b"\x89PNG\r\n\x1a\n" + b"x" * 8),
            "image/png",
        )

    def test_a_large_image_is_still_an_image(self):
        self.assertEqual(
            highlight.view("a.png", b"\x89PNG\r\n\x1a\n" + b"x" * 1_000_000)[
                "kind"
            ],
            "image",
        )
        large = highlight.view(
            "a.m", b"x" * (highlight.MAX_VIEW_BYTES + 1), "octave"
        )
        self.assertEqual((large["kind"], large["tokens"]), ("large", []))

    def test_empty_file(self):
        v = highlight.view("a.m", b"", "octave")
        self.assertEqual((v["kind"], v["lines"]), ("text", 0))


if __name__ == "__main__":
    unittest.main()
