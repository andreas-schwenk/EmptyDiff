# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Provided functions of a lab: functions that the system puts into the
run folder so that the code of the students can call them (for example a
solution of an earlier task that a later task builds on).

The lab stores them as one text. How the text is cut into files depends
on the language of the lab and is done by its adapter (`languages/`); a
student's file of the same name wins. Standard library only.
"""

from . import languages
from .languages import (
    ProvidedError,
)  # noqa: F401  (raised by the adapters, caught by labs, backup and web)

MAX_CHARS = 200_000


def split(language, text):
    """The text as [(function name, source)] in the order of the text.
    Raises ProvidedError."""
    return languages.get(language).split_provided(text)


def files(language, text):
    """{file name: bytes} for the run folder, or {} for an empty text.
    Raises ProvidedError."""
    return languages.get(language).provided_files(text)


def names(language, text):
    """File names of the provided functions; empty if the text cannot be
    read (shown to students, never fails)."""
    try:
        return sorted(files(language, text))
    except (ProvidedError, languages.UnknownLanguage):
        return []
