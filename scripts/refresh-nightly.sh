#!/bin/bash
# ---------------------------------------------------------------------------
# Päkpätät -- nightly archive check.
#
# Runs unattended from launchd. It does everything EXCEPT publish:
#
#     detect  -> is anything different on help.unhcr.org?
#     fetch   -> if so, pull just those pages into the archive
#     build   -> rebuild corpus + index into data/staging/
#     diff    -> write a report, shouting about changed phone numbers and fees
#     notify  -> tell the operator there is something to review
#
# It never promotes. `05_intelligence/change_watch/README.md` requires a human
# to read a changed hotline before case workers start repeating it, and an
# unattended job that published its own scrape would delete that step. The
# operator promotes with:  python pipeline/refresh.py promote
#
# On a normal night nothing has changed, this makes 3 requests, downloads a few
# KB, and exits silently.
# ---------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PY="$ROOT/.venv/bin/python"
LOG="$ROOT/data/refresh.log"
mkdir -p "$ROOT/data"

# Load the operator's settings (PAKPATAT_ARCHIVE lives here). launchd gives a
# near-empty environment, so nothing can be assumed to be inherited.
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

notify() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"Päkpätät\"" 2>/dev/null || true
}

# Jitter. launchd fires every machine at the same wall-clock second; a random
# wait up to 30 minutes means a fleet of these does not arrive as a spike, and
# the request pattern does not look like clockwork. Skipped when a human is
# running it by hand, who should not sit watching a blank screen for 30 minutes.
[ -n "${PAKPATAT_SKIP_JITTER:-}" ] || sleep $(( RANDOM % 1800 ))

log "=== nightly check starting ==="

if [ ! -x "$PY" ]; then
  log "ERROR: no virtualenv at $PY -- run scripts/start-macos.command once first"
  exit 1
fi
if [ -z "${PAKPATAT_ARCHIVE:-}" ]; then
  log "ERROR: PAKPATAT_ARCHIVE is not set (put it in .env) -- nothing to refresh"
  exit 1
fi

out="$("$PY" pipeline/refresh.py detect 2>&1)"
echo "$out" >> "$LOG"

if ! grep -q "need fetching" <<<"$out"; then
  log "no changes; done"
  exit 0
fi

n="$(grep -oE '^[0-9]+ page\(s\) need fetching' <<<"$out" | grep -oE '^[0-9]+')"
log "$n page(s) changed -- fetching"

"$PY" pipeline/refresh.py fetch >> "$LOG" 2>&1 || { log "fetch failed"; notify "Archive check: fetch failed"; exit 1; }
"$PY" pipeline/refresh.py build >> "$LOG" 2>&1 || { log "build failed"; notify "Archive check: build failed"; exit 1; }

report="$ROOT/data/refresh_report.txt"
"$PY" pipeline/refresh.py diff --text > "$report" 2>&1
cat "$report" >> "$LOG"

if grep -q "CRITICAL-FACT CHANGE" "$report"; then
  log "CRITICAL fact change detected -- operator must review"
  notify "$n page(s) changed, including a PHONE/FEE/EMAIL. Review before sharing."
else
  log "$n page(s) changed, no critical facts affected"
  notify "$n archive page(s) changed. Staged and ready to review."
fi

log "=== staged; awaiting 'refresh.py promote' ==="
