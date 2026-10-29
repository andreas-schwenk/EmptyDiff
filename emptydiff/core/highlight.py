# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Presentation of submitted files: text with simple syntax
highlighting, images, binary data (standard library only).

Splits text into segments (kind, text); the presentation (HTML, colors)
lives in the template and CSS. The tokenizer of a language is part of
its adapter (`languages/`); it is deliberately plain: comments, strings,
numbers, keywords and a few built-in functions.
"""

from . import languages

MAX_VIEW_BYTES = 200_000


def decode(content):
    """Text of a student file; Windows files (cp1252) with umlauts stay
    readable. None for binary data."""
    if b"\x00" in content[:4096]:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


IMAGE_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "bmp",
    "webp",
}  # SVG is left out on purpose: it can carry scripts


def image_type(name, content):
    """The media type (e.g. 'image/png') if `name` has an image
    extension and the data really starts like that image, otherwise
    None. Never trusts the extension alone."""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in IMAGE_EXTENSIONS:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:2] == b"BM" and extension == "bmp":
        return "image/bmp"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def view(name, content, language=None):
    """Presentation of a submitted file: {name, size, kind, tokens,
    lines, mime}.

    kind is 'text', 'image', 'binary' or 'large' (only text has tokens;
    an image has its media type in `mime`). Only files with the
    extension of `language` (the identifier of the lab's language) are
    highlighted.
    """
    info = {
        "name": name,
        "size": len(content),
        "kind": "text",
        "tokens": [],
        "lines": 0,
        "mime": None,
    }
    mime = image_type(name, content)
    if mime:
        info.update(kind="image", mime=mime)
        return info
    if len(content) > MAX_VIEW_BYTES:
        info["kind"] = "large"
        return info
    text = decode(content)
    if text is None:
        info["kind"] = "binary"
        return info
    text = text.rstrip("\n")
    info["lines"] = text.count("\n") + 1 if text else 0
    info["tokens"] = _tokens(name, text, language)
    return info


def _tokens(name, text, language):
    try:
        adapter = languages.get(language) if language else None
    except languages.UnknownLanguage:
        adapter = None
    if (
        adapter is not None
        and adapter.extension
        and name.lower().endswith(adapter.extension)
    ):
        return adapter.tokens(text)
    return [(None, text)]
