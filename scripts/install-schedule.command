#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
# ---------------------------------------------------------------------------
# Turn the nightly archive check on or off.
#
#   Double-click        -> install and start it
#   ./install-schedule.command off   -> stop and remove it
#   ./install-schedule.command test  -> run one check right now, in this window
#
# Nothing here publishes anything. The nightly job stages changes and tells you;
# you decide whether they go live with 'refresh.py promote'.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

LABEL="com.pakpatat.refresh"
AGENTS="$HOME/Library/LaunchAgents"
TARGET="$AGENTS/$LABEL.plist"
ACTION="${1:-on}"

case "$ACTION" in
  off)
    launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Nightly archive check removed."
    ;;

  test)
    echo "Running one check now (this is exactly what the nightly job runs,"
    echo "minus the random wait). Ctrl-C to stop."
    echo
    PAKPATAT_SKIP_JITTER=1 bash scripts/refresh-nightly.sh
    echo
    echo "Log: $ROOT/data/refresh.log"
    ;;

  on)
    if [ ! -x "$ROOT/.venv/bin/python" ]; then
      echo "Set the app up first: double-click scripts/start-macos.command"
      exit 1
    fi
    if ! grep -qs "^PAKPATAT_ARCHIVE=" "$ROOT/.env" 2>/dev/null; then
      echo "-----------------------------------------------------------"
      echo "PAKPATAT_ARCHIVE is not set in .env, so the nightly job will"
      echo "not know where your archive lives. Add a line like:"
      echo
      echo "    PAKPATAT_ARCHIVE=$HOME/Desktop/refugee_malaysia"
      echo
      echo "to the file '.env' in this folder, then run this again."
      echo "-----------------------------------------------------------"
      exit 1
    fi

    mkdir -p "$AGENTS"
    sed "s|__ROOT__|$ROOT|g" scripts/com.pakpatat.refresh.plist > "$TARGET"
    chmod +x scripts/refresh-nightly.sh
    launchctl unload "$TARGET" 2>/dev/null || true
    launchctl load "$TARGET"

    echo "Nightly archive check installed."
    echo
    echo "  runs      : 03:10 each night, plus a random 0-30 min wait"
    echo "  cost      : ~3 requests when nothing has changed"
    echo "  on change : fetches, stages, and notifies you -- publishes NOTHING"
    echo "  log       : data/refresh.log"
    echo
    echo "To publish reviewed changes:  .venv/bin/python pipeline/refresh.py promote"
    echo "To turn it off:               ./scripts/install-schedule.command off"
    ;;

  *)
    echo "Usage: $0 [on|off|test]"; exit 1 ;;
esac
