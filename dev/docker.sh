#!/bin/sh
# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

# Helper script for the Docker development environment (local only, not part of the production system).
#   dev/docker.sh build     build the image
#   dev/docker.sh web       development server on http://127.0.0.1:8091 (container only while it runs)
#   dev/docker.sh test      unit tests in the container
#   dev/docker.sh sandbox   sandbox check (bubblewrap + Octave)
#   dev/docker.sh lint      style check of the Python source (black --check and pycodestyle, line length 79)
#   dev/docker.sh format    format the Python source with black
#   dev/docker.sh shell     shell in the container
#   dev/docker.sh up        start the persistent container 'emptydiff-dev' including the web server (visible in Docker Desktop)
#   dev/docker.sh serve     (re)start the web server in the persistent container
#   dev/docker.sh exec ...  run a command in the persistent container (default: bash)
#   dev/docker.sh down      stop and remove the persistent container
#   dev/docker.sh run ...   run any command in a one-off container
set -eu
cd "$(dirname "$0")/.."

IMAGE=emptydiff-dev
NAME=emptydiff-dev
# Host port: not 5000, which the AirPlay receiver occupies on macOS (it answers with 403, especially
# via "localhost" = ::1). Can be changed with EMPTYDIFF_DEV_PORT.
PORT=${EMPTYDIFF_DEV_PORT:-8091}
# seccomp=unconfined: Docker blocks the user namespaces that bwrap needs by default.
# systempaths=unconfined: Docker masks /proc, otherwise bwrap cannot mount its own proc.
# Both only apply to this development environment. They are not needed on the Debian VM.
# --init: a small init process as PID 1 that reaps orphaned processes (zombies). Without it,
# zombies pile up after aborted runs and count against the process limit (prlimit --nproc).
# On the VM systemd takes over this job.
OPTS="--init --security-opt seccomp=unconfined --security-opt systempaths=unconfined -v $PWD:/app"

cmd=${1:-}
[ $# -gt 0 ] && shift

case "$cmd" in
  build)   docker build -t "$IMAGE" -f dev/Dockerfile dev ;;
  web)     exec docker run --rm $OPTS -it -p "127.0.0.1:$PORT:5000" "$IMAGE" ;;
  test)    exec docker run --rm $OPTS "$IMAGE" python3 -m unittest discover -s tests -v ;;
  sandbox) exec docker run --rm $OPTS "$IMAGE" sh dev/check-sandbox.sh ;;
  lint)    exec docker run --rm $OPTS "$IMAGE" sh -c 'black --check --quiet emptydiff tests dev && python3 -m pycodestyle emptydiff tests dev && echo "Style check passed."' ;;
  format)  exec docker run --rm $OPTS "$IMAGE" black --quiet emptydiff tests dev ;;
  shell)   exec docker run --rm $OPTS -it "$IMAGE" bash ;;
  run)     exec docker run --rm $OPTS "$IMAGE" "$@" ;;
  up)      docker run -d --name "$NAME" $OPTS -p "127.0.0.1:$PORT:5000" "$IMAGE" sleep infinity >/dev/null
           "$0" serve
           echo "Container '$NAME' is running (web: http://127.0.0.1:$PORT). Commands: dev/docker.sh exec <command>" ;;
  serve)   # Without a running web server Docker accepts connections and returns an empty response.
           docker exec "$NAME" pkill -f "emptydiff web" 2>/dev/null || true
           docker exec -d "$NAME" python3 -m emptydiff web --host 0.0.0.0
           for _ in 1 2 3 4 5 6 7 8 9 10; do
             if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null; then echo "Web server ready."; exit 0; fi
             sleep 1
           done
           echo "Web server does not respond (logs: docker logs $NAME)" >&2; exit 1 ;;
  exec)    [ $# -eq 0 ] && set -- bash
           if [ -t 0 ]; then exec docker exec -it "$NAME" "$@"; else exec docker exec -i "$NAME" "$@"; fi ;;
  down)    docker rm -f "$NAME" ;;
  *)       sed -n '2,13p' "$0"; exit 1 ;;
esac
