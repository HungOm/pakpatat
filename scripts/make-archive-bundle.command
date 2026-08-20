#!/bin/bash
# ---------------------------------------------------------------------------
# Päkpätät -- package the un-crawlable half of the archive for another machine
#
# Double-click this, or run it from a terminal, on the machine that HOLDS the
# archive. It produces one .tar.gz and prints the two lines the receiving
# install needs in its .env.
#
# WHY THIS EXISTS RATHER THAN "just zip the folder"
# --------------------------------------------------
# Zipping the folder instead of its contents puts everything one level too
# deep, and a bundle like that unpacks without error and produces an app that
# answers "that is not in the archive" to every question. The app now detects
# that case and says so (pakpatat/bundle.py), but not making the mistake beats
# diagnosing it. This script always packs from INSIDE the archive root.
#
# TWO SIZES, AND THE DIFFERENCE IS 500x
# -------------------------------------
#     ./make-archive-bundle.command          text  (default)  ~3.6 MB
#     ./make-archive-bundle.command full     everything       ~1.8 GB
#
# They produce IDENTICAL search results. Every retired-site passage the app can
# answer from comes out of the 30 index.md files, which total 214 KB; the
# corpus builder never opens page.html or resources/ at all (see
# pakpatat/corpus.py old_docs()). The other 1.8 GB is training decks and
# posters -- 414 MB for one financial-literacy PDF alone -- that this app
# cannot read a word of.
#
# So `text` is the default, because a 3.6 MB file goes through any link and a
# 1.8 GB one does not. Use `full` when you are handing someone the whole
# archive to keep, not when you are trying to make their app work.
#
# WHAT GOES IN, AND WHAT DELIBERATELY DOES NOT
# --------------------------------------------
# In:   01_support_topics/     the retired refugeemalaysia.org capture. The site
#                              was taken down on 2026-07-14 -- for the NGO
#                              clinic directory, Verify Plus and the refugee
#                              lexicon, copies like yours are the only ones
#                              that exist. `text` takes the index.md files;
#                              `full` takes the PDFs and posters too.
#       07_partner_materials/  handed to you directly, never published. Taken
#                              whole in both modes -- the corpus reads these
#                              files directly, so they are not optional.
#       05_intelligence/gap_analysis/
#                              which retired pages the live site replaced. The
#                              ranking needs it to put live guidance first.
#
# Out:  04_help_unhcr_2026/    the live site. The receiving app crawls this for
#                              itself in about two minutes, so shipping it only
#                              makes the bundle bigger and staler.
#
# BEFORE YOU SEND THIS TO ANYONE
# ------------------------------
# The pages inside are UNHCR's copyrighted work. Handing them to another
# machine in your own organisation is not the same as publishing them. Do not
# put the result on a public link. Read NOTICE.md.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")/.."

printf '\033[38;5;99m'
cat <<'OWL'
        ,___,   ,___,
        [O,O]   [O,O]
        /)__)   /)__)
    ----"--"-----"--"----
OWL
printf '\033[0m'
echo "    Päkpätät  -  packaging an archive bundle"
echo "-----------------------------------------------------------"
echo

PY="${PYTHON:-python3}"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

ROOT="${PAKPATAT_ARCHIVE:-}"
if [ -z "$ROOT" ]; then
  ROOT="$("$PY" -c 'from pakpatat import config; print(config.ARCHIVE_ROOT or "")' 2>/dev/null || true)"
fi
if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
  echo "Cannot find your archive."
  echo "Set PAKPATAT_ARCHIVE to the folder that contains 01_support_topics/, e.g."
  echo "    PAKPATAT_ARCHIVE=~/Desktop/refugee_malaysia $0"
  exit 1
fi
MODE="${1:-text}"
case "$MODE" in
  text|full) ;;
  *) echo "Usage: $0 [text|full]"; exit 1 ;;
esac
echo "Archive: $ROOT"
echo "Mode:    $MODE"
echo

# --- 1. Build the file list ------------------------------------------------
#
# Written to a list and fed to tar with -T rather than globbed, because `text`
# mode takes 30 specific files out of a 1.8GB tree and a shell glob cannot
# express that. Paths are relative to $ROOT so the tar unpacks with
# 01_support_topics/ at its top level -- see the note about nesting above.
LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT

if [ -d "$ROOT/01_support_topics" ]; then
  if [ "$MODE" = "text" ]; then
    ( cd "$ROOT" && find 01_support_topics -name 'index.md' ) >> "$LIST"
    echo "  include  01_support_topics/*/index.md  ($(grep -c . "$LIST") files, the searchable text)"
  else
    ( cd "$ROOT" && find 01_support_topics -type f ) >> "$LIST"
    echo "  include  01_support_topics/  (everything, $(du -sh "$ROOT/01_support_topics" | cut -f1))"
  fi
else
  echo "  absent   01_support_topics"
fi

for d in 07_partner_materials 05_intelligence/gap_analysis; do
  if [ -d "$ROOT/$d" ]; then
    ( cd "$ROOT" && find "$d" -type f ) >> "$LIST"
    echo "  include  $d/  ($(find "$ROOT/$d" -type f | wc -l | tr -d ' ') files, $(du -sh "$ROOT/$d" | cut -f1 | tr -d ' '))"
  else
    echo "  absent   $d"
  fi
done

if [ ! -s "$LIST" ]; then
  echo
  echo "None of the un-crawlable parts are present, so a bundle would be empty."
  echo "The receiving app can already crawl the live site by itself."
  exit 1
fi
echo
echo "  skipped  04_help_unhcr_2026  (the receiving app crawls this itself)"
if [ "$MODE" = "text" ]; then
  echo "  skipped  page.html and resources/  (the app never reads them --"
  echo "           pass 'full' if you are handing over the whole archive)"
fi
echo

# --- 2. The rights question, asked out loud --------------------------------
echo "-----------------------------------------------------------"
echo "This bundle contains guidance published by UNHCR and its partners,"
echo "plus material given to you directly. Sending it to another machine in"
echo "your organisation is a handoff. Putting it on a public link is"
echo "publishing, and that is UNHCR's decision to make, not yours."
echo "Read NOTICE.md."
echo "-----------------------------------------------------------"
printf "Type 'bundle' to continue: "
read -r reply
[ "$reply" = "bundle" ] || { echo "Aborted."; exit 1; }

# --- 3. Pack, always from inside the root ----------------------------------
STAMP="$(date +%Y%m%d)"
OUT="$PWD/dist/pakpatat-archive-${MODE}-${STAMP}.tar.gz"
mkdir -p "$PWD/dist"

echo
echo "Packing..."
# -C "$ROOT" is the whole point: paths inside the tar start at
# 01_support_topics/, not <foldername>/01_support_topics/.
tar -C "$ROOT" -czf "$OUT" -T "$LIST"

SHA="$(shasum -a 256 "$OUT" | cut -d' ' -f1)"
SIZE="$(du -h "$OUT" | cut -f1 | tr -d ' ')"

# --- 4. Prove it unpacks into something the app recognises -----------------
#
# The same check the app runs on download, run here, so a bad bundle is caught
# by the person who can still fix it rather than by someone on another machine.
echo "Verifying the bundle unpacks into a valid archive..."
TMP="$(mktemp -d)"
tar -C "$TMP" -xzf "$OUT"
"$PY" - "$TMP" <<'PYCHECK'
import sys, pathlib
sys.path.insert(0, ".")
from pakpatat import bundle
try:
    found = bundle._check_layout(pathlib.Path(sys.argv[1]))
except bundle.Unavailable as e:
    print(f"  FAILED: {e}")
    raise SystemExit(1)
for p in found["parts"]:
    if p["present"]:
        print(f"  OK  {p['label']}: {p['files']} files")
PYCHECK
rm -rf "$TMP"

echo
echo "-----------------------------------------------------------"
echo "Built: $OUT  ($SIZE)"
echo
echo "On the receiving machine, put these in its .env:"
echo
echo "    PAKPATAT_ARCHIVE_BUNDLE=<https URL you upload this to>"
echo "    PAKPATAT_ARCHIVE_SHA256=$SHA"
echo
echo "Then press 'Get the archive' in the app. It fetches this bundle, checks"
echo "it against that digest, crawls the live site, and indexes both."
echo
echo "Upload it somewhere that needs a credential and serves the FILE, not a"
echo "preview page. If the host needs a token, set PAKPATAT_ARCHIVE_TOKEN too."
echo "-----------------------------------------------------------"
