# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Texts in several languages (DESIGN.md section 21). Standard library
only.

All texts of the interface live in the files `emptydiff/strings/*.json`,
one per area (common, student, admin, forms, backup, privacy, errors):
`{"DE": {"key": "text"}, "EN": {...}}`. The files are merged; a key may
exist in one file only. A new language is another block in these files,
or one file of its own (do not forget the key `lang.name`). If a key is
missing in a language, the English text is used, then the German one,
finally the key itself.

Placeholders are written `{name}`. Keys ending in `_html` contain
trusted HTML (only allowed in the web interface, where the parameters
are escaped). Plural: `key_one` and `key_other`.
"""

import glob
import json
import os

DIRECTORY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "strings"
)
FALLBACKS = ("EN", "DE")
OPERATOR_LANGUAGE = (
    "EN"  # language of str(error) for logs and the command line
)

_cache = None


def strings():
    """Load all files of the strings directory once, merge them and
    check the structure."""
    global _cache
    if _cache is None:
        data = {}
        owner = (
            {}
        )  # (language, key) -> file, to find a key that two files define
        for path in sorted(glob.glob(os.path.join(DIRECTORY, "*.json"))):
            name = os.path.basename(path)
            with open(path, encoding="utf-8") as f:
                blocks = json.load(f)
            if not isinstance(blocks, dict) or not all(
                isinstance(v, dict)
                and all(
                    isinstance(k, str) and isinstance(t, str)
                    for k, t in v.items()
                )
                for v in blocks.values()
            ):
                raise ValueError(
                    f"strings/{name}: expected {{language: {{key: text}}}}"
                )
            for lang, texts in blocks.items():
                for key, text in texts.items():
                    if (lang, key) in owner:
                        raise ValueError(
                            f"strings: {lang}:{key} is defined in "
                            f"{owner[(lang, key)]} and in {name}"
                        )
                    owner[(lang, key)] = name
                    data.setdefault(lang, {})[key] = text
        if not data:
            raise ValueError("strings: no texts found")
        _cache = data
    return _cache


def languages():
    """Available languages in the order of the file, e.g. ['DE',
    'EN']."""
    return list(strings())


def language_name(lang):
    return lookup(lang, "lang.name") if lang in strings() else lang


def lookup(lang, key):
    """The raw text for a key (fallback: English, German, the key
    itself)."""
    data = strings()
    for candidate in (lang, *FALLBACKS):
        text = data.get(candidate, {}).get(key)
        if text is not None:
            return text
    return key


def translate(lang, key, **params):
    text = lookup(lang, key)
    return text.format(**params) if params else text


def plural(lang, key, n, **params):
    """Choose `<key>_one` for n == 1, otherwise `<key>_other`; `{n}` is
    available as a parameter."""
    return translate(
        lang, f"{key}_one" if n == 1 else f"{key}_other", n=n, **params
    )


def best_language(accepted, default):
    """Choose the language from a list (language code, weight),
    descending by weight, e.g. [("de-DE", 1.0)].

    Codes of equal length are compared with the primary language
    ("de-AT" matches DE). Without a match `default` is used.
    """
    available = {code.lower(): code for code in languages()}
    for tag, _quality in sorted(accepted, key=lambda item: -item[1]):
        primary = tag.lower().split("-")[0]
        if primary in available:
            return available[primary]
    codes = languages()
    return (
        default if default in codes else ("EN" if "EN" in codes else codes[0])
    )


class LocalizedError(Exception):
    """Error with a translatable message: `Error("key", name=value)`;
    `message(lang)` translates."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def message(self, lang):
        return translate(lang, self.key, **self.params)

    def __str__(self):
        return self.message(OPERATOR_LANGUAGE)
