# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

# Development environment (NOT part of the production system, see DESIGN.md 13.1).
# Mirrors the Debian 13 VM: same package sources for Python, Flask, bubblewrap and Octave.
# gnuplot-nox and fonts-freefont-otf: without a display Octave only knows the gnuplot toolkit and
# needs a font; without both, figure/plot fail in student code ("no graphics
# toolkits are available"). Both packages belong on the VM as well.
# black and python3-pycodestyle: format and style check of the Python source (dev/docker.sh lint); development only.
FROM debian:trixie-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-flask bubblewrap octave coreutils util-linux procps \
      gnuplot-nox fonts-freefont-otf \
      black python3-pycodestyle \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user as on the VM (bwrap must work without root)
RUN useradd --create-home --uid 1000 emptydiff
USER emptydiff
WORKDIR /app

ENV EMPTYDIFF_DEV=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 5000
CMD ["python3", "-m", "emptydiff", "web", "--host", "0.0.0.0"]
