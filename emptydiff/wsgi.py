# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

"""WSGI entry point for Apache/mod_wsgi (daemon mode). The configuration
comes from /etc/emptydiff/config.toml.

mod_wsgi loads this file as a standalone script, hence the absolute
import (python-path points to the installation directory).
"""

from emptydiff.web import create_app

application = create_app()
