#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
# ---------------------------------------------------------------------------
# Päkpätät -- build a macOS .app / .dmg that is ready to use on arrival
#
# Double-click this file, or run it from a terminal, on the Mac that holds the
# archive. It produces dist/Pakpatat-<version>-macOS-<arch>.dmg.
#
# The point of this script is the word READY. A default build ships the program
# and nothing else, so the person who installs it opens a window with two red
# rows and no way to act on either. This one bundles:
#
#   the archive         data/corpus.jsonl + data/index/   (your decision -- see
#                                                          the prompt below)
#   the search model    data/.models/                     (~220MB, so the first
#                                                          search works offline)
#
# and then PROVES the result by running the frozen app's own preflight against
# an empty data directory -- the closest thing to installing it on a machine
# that has never seen this project.
#
# What it cannot bundle: the answering model. That is ~2GB and lives inside
# Ollama, a separate application. The app now downloads it from a button on its
# first screen, which is the closest to automatic that step can honestly get.
# ---------------------------------------------------------------------------
set -e
cd "$(dirname "$0")/.."

printf '\033[38;5;99m'
cat <<'OWL'
        ,___,   ,___,
        [O,O]   [O,O]
        /)__)   /)__)
    ----"--"-----"--"----
OWL
printf '\033[0m'
echo "    Päkpätät  -  building a macOS app"
echo "-----------------------------------------------------------"
echo

if [ "$(uname)" != "Darwin" ]; then
  echo "This builds a .app, so it has to run on macOS."
  exit 1
fi

# --- 1. Environment -------------------------------------------------------
if [ ! -d ".venv" ]; then
  echo "No .venv yet -- run scripts/start-macos.command once first."
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pyinstaller

VERSION="$(python packaging/version_info.py --print)"
echo "Version $VERSION"

# --- 2. The archive question ---------------------------------------------
#
# Asked out loud, every time, because it is a rights decision and not a build
# setting. The source pages are UNHCR's; handing them to someone else is a
# choice only the operator can make (NOTICE.md). Answering "no" still produces
# a working app -- it just arrives empty, and its first screen says so.
CORPUS="$(python -c 'from pakpatat import config; print(config.CORPUS)')"
BUNDLE_ARCHIVE=0
if [ -f "$CORPUS" ]; then
  echo
  echo "-----------------------------------------------------------"
  echo "Include the archive in the app?"
  echo
  echo "  Yes -> whoever installs this can search immediately."
  echo "  No  -> they install a working app with nothing in it, and"
  echo "         its first screen tells them to ask you for the data."
  echo
  echo "The archive is guidance published by UNHCR and its partners."
  echo "Distributing it is UNHCR's decision to make, not this tool's."
  echo "Read NOTICE.md before answering yes."
  echo "-----------------------------------------------------------"
  printf "Type 'include' to bundle the archive, anything else to skip: "
  read -r reply
  if [ "$reply" = "include" ]; then
    BUNDLE_ARCHIVE=1
    echo "Bundling the archive."
  else
    echo "Building without the archive."
  fi
else
  echo
  echo "No corpus at $CORPUS -- building the program only."
fi

# --- 3. The search model --------------------------------------------------
#
# Bundled by the spec if it is cached, skipped silently if it is not -- and a
# .dmg missing it needs 220MB of internet before it can answer anything, which
# is the opposite of the point. So fetch it here rather than find out later.
echo
echo "Checking the search model is cached..."
python - <<'PY'
import os
from pakpatat import config
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(config.EMBED_CACHE))
if config.EMBED_CACHE.is_dir() and any(config.EMBED_CACHE.glob("models--*")):
    print("  already cached ->", config.EMBED_CACHE)
else:
    print("  downloading ~220MB, once...")
    from fastembed import TextEmbedding
    TextEmbedding(config.EMBED_MODEL)
    print("  cached ->", config.EMBED_CACHE)
PY

# --- 4. Build -------------------------------------------------------------
echo
echo "Building the .app (a few minutes)..."
if [ "$BUNDLE_ARCHIVE" = "1" ]; then
  PAKPATAT_BUNDLE_ARCHIVE=1 pyinstaller packaging/pakpatat-macos.spec --noconfirm
else
  pyinstaller packaging/pakpatat-macos.spec --noconfirm
fi

APP="dist/Pakpatat.app/Contents/MacOS/Pakpatat"

# --- 5. Prove it ----------------------------------------------------------
#
# Two separate questions, and shipping has failed on both before:
#   selftest   can the frozen app find its own UI and providers?
#   preflight  on a machine that has never run it, does it have anything to
#              answer WITH? Pointed at a throwaway data directory, so the
#              first-run seeding from the bundle is what gets tested rather
#              than this developer's own data/.
echo
echo "Checking the app can find its own parts..."
"$APP" --selftest

FRESH="$(mktemp -d)/first-run"
echo
echo "Checking what a fresh install would report..."
echo "  (using a throwaway data folder: $FRESH)"
set +e
PAKPATAT_DATA="$FRESH" "$APP" --preflight
FRESH_READY=$?
set -e

# --- 6. The .dmg ----------------------------------------------------------
ARCH="$(lipo -archs "$APP" | tr -d ' ')"
DMG="dist/Pakpatat-${VERSION}-macOS-${ARCH}.dmg"
echo
echo "Packaging $DMG ..."
rm -rf build/dmg && mkdir -p build/dmg
cp -R dist/Pakpatat.app build/dmg/
ln -s /Applications build/dmg/Applications
hdiutil create -volname "Pakpatat ${VERSION}" -srcfolder build/dmg \
  -ov -format UDZO "$DMG" >/dev/null
rm -rf "$(dirname "$FRESH")"

echo
echo "-----------------------------------------------------------"
echo "Built: $DMG"
if [ "$FRESH_READY" = "0" ]; then
  echo "A fresh install has its archive, index and search model ready."
  echo "The answering model is the one thing this check cannot speak for:"
  echo "it read THIS computer's Ollama. A recipient without Ollama gets a"
  echo "'Get the offline AI engine' button on the app's first screen."
else
  echo "A fresh install of this .dmg will open with red rows -- see the"
  echo "check list above. That is fine if you meant to ship it empty."
fi
echo
echo "It is NOT code-signed: the first launch needs right-click -> Open."
echo "-----------------------------------------------------------"
