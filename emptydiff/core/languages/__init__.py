# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""Programming languages that a lab can use (DESIGN.md section 20.1).
Standard library only.

Everything that depends on the language of a lab lives in one adapter
class per language (see `Language`): how the testbench is put together
and started, how provided functions become files, which words are
conspicuous, how source code is highlighted. The rest of the system
(sandbox, run folder, results protocol, verdict, queue) is the same for
every language. A language is registered at the bottom of this file;
which of the registered languages a server offers is the operator's
choice (`languages.enabled` in the configuration).

Texts of a language (help for lecturers, the starting text of a
testbench, its error messages) live in the strings file
`emptydiff/strings/lang_<id>.json` under the keys `ln.<id>.*`.
"""

from .. import i18n


class LanguageError(i18n.LocalizedError):
    """Something about a language is not usable (message readable by
    lecturers)."""


class TestbenchError(LanguageError):
    """The text of a testbench (or its helper functions) is not usable
    for the language."""


class ProvidedError(LanguageError):
    """The text of the provided functions is not usable for the
    language."""


class UnknownLanguage(LanguageError):
    """No adapter exists for this language identifier."""

    def __init__(self, language):
        super().__init__("err.lang.unknown", language=language)


class Language:
    """Contract of a language adapter. Subclass it, fill in the
    attributes and override the methods."""

    id = ""  # short identifier stored in the database, e.g. 'octave'
    name = ""  # name shown to lecturers, e.g. 'Octave/MATLAB'
    extension = ""  # extension of source files, e.g. '.m'
    # name of the interpreter in the sandbox (`languages.<id>.command`)
    default_command = ""
    # words that mark a submission as conspicuous
    # (`languages.<id>.suspicious_words`)
    default_suspicious_words = ()
    packages = ()  # Debian packages the language needs on the server
    # more paths of the host that the sandbox may read (besides /usr)
    extra_ro_binds = ()
    # name of the file the grader writes and starts; students cannot
    # require it
    testbench_file = ""
    selftest_body = ""  # a testbench body that must pass, for the self-test

    def files(self, body, helpers, results_file, token):
        """{file name: bytes}: the files the grader writes into the run
        folder for the testbench.

        `results_file` is the name of the file in the run folder to
        which every check appends the line
        `<token>\\t<ok|fail|done>\\t<text>`; `token` is a random string
        that every recorded line has to carry.
        """
        raise NotImplementedError

    def argv(self, command):
        """The command line of the interpreter, `command` being its
        configured name, started in the run folder."""
        raise NotImplementedError

    def check_testbench(self, body):
        """Raise TestbenchError if the body cannot be used (the system
        adds the frame around it)."""

    def has_check(self, text):
        """Whether the text calls the checking functions at all (a hint
        for lecturers, not an error)."""
        return True

    def split_provided(self, text):
        """The text of the provided functions as [(name, source)]; raise
        ProvidedError if it is not usable."""
        return []

    def provided_file_name(self, name):
        return name + self.extension

    def provided_files(self, text):
        """{file name: bytes} for the run folder, or {} for an empty
        text. Raises ProvidedError."""
        return {
            self.provided_file_name(name): source.encode("utf-8")
            for name, source in self.split_provided(text)
        }

    def tokens(self, text):
        """Source text as [(kind, text)]; kind is None for text that is
        not highlighted."""
        return [(None, text)]


_REGISTRY = {}


def register(language):
    _REGISTRY[language.id] = language
    return language


def get(language_id):
    """The adapter of a language; UnknownLanguage if there is none."""
    try:
        return _REGISTRY[language_id]
    except KeyError:
        raise UnknownLanguage(language_id) from None


def ids():
    """Identifiers of all registered languages (the code knows them, a
    server need not offer them)."""
    return list(_REGISTRY)


def defaults():
    """Default configuration of the section `languages` (see
    config.py)."""
    section = {"enabled": [DEFAULT]}
    for language in _REGISTRY.values():
        section[language.id] = {
            "command": language.default_command,
            "suspicious_words": list(language.default_suspicious_words),
        }
    return section


def enabled(cfg):
    """Identifiers of the languages this server offers, in the
    configured order."""
    return list(cfg["languages"]["enabled"])


from . import (
    octave,
)  # noqa: E402  (registers itself; the adapters import the classes above)

DEFAULT = octave.OCTAVE.id  # language of a new lab if nothing else is chosen
