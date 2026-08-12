#!/bin/bash
# ---------------------------------------------------------------------------
# Päkpätät -- macOS / Linux launcher
#
# Double-click this file to start the assistant.
# The first run sets everything up (a few minutes). Later runs open instantly.
#
# If macOS blocks it the first time: right-click -> Open -> Open.
# ---------------------------------------------------------------------------
set -e
cd "$(dirname "$0")/.."

# Branded splash, printed BEFORE anything slow. A double-click that shows a
# bare cursor for forty seconds while pip resolves reads as broken; the owl
# lands in under a second and the phases below narrate the rest.
#
# Drawn in the shell rather than read from pakpatat/brand.py on purpose: at
# this point in the script there may be no virtualenv and no Python yet.
printf '\033[38;5;99m'           # indigo, on the 256-colour ramp
cat <<'OWL'
        ,___,   ,___,
        [O,O]   [O,O]
        /)__)   /)__)
    ----"--"-----"--"----
OWL
printf '\033[0m'
echo "    Päkpätät  -  K'Cho for owl"
echo "    It answers what it knows. It says so when it doesn't."
echo
echo "    Independent tool. Not affiliated with UNHCR."
echo "-----------------------------------------------------------"
echo

# --- 1. Find Python 3.11+ -------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "ERROR: Python 3.11 or newer is required but was not found."
  echo
  echo "Install it from https://www.python.org/downloads/"
  echo "then double-click this file again."
  echo
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi
echo "Using $($PY --version)"

# --- 2. Create the private environment (first run only) -------------------
if [ ! -d ".venv" ]; then
  echo
  echo "First-time setup: creating a private Python environment..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# --- 3. Install dependencies (only when requirements change) --------------
STAMP=".venv/.installed"
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing components (this can take a few minutes the first time)..."
  python -m pip install --upgrade pip --quiet
  python -m pip install -r requirements.txt --quiet
  touch "$STAMP"
  echo "Components installed."
fi

# --- 4. Build the offline search index (first run only) -------------------
#
# Ask config.py where the index actually lives rather than hardcoding a path.
# An earlier version checked ".index/meta.json", which config.py has never
# written -- the real default is "data/index/meta.json", and PAKPATAT_DATA can
# move it anywhere. The check therefore never passed, so EVERY launch silently
# re-embedded the whole corpus and the "later runs open instantly" promise at
# the top of this file was false.
INDEX_META="$(python -c 'from pakpatat import config; print(config.INDEX_META)')"
CORPUS="$(python -c 'from pakpatat import config; print(config.CORPUS)')"
if [ ! -f "$INDEX_META" ]; then
  # No index AND no corpus means this copy was handed over without an archive.
  # The repository ships code only (see NOTICE.md), so a fresh copy has nothing
  # to search. Previously build_index.py exited here with "Corpus not found:
  # /long/path/corpus.jsonl -- run pipeline/build_corpus.py first", `set -e`
  # closed the window, and a case worker was left with a file path and no idea
  # what to do. Say something they can act on instead.
  if [ ! -f "$CORPUS" ]; then
    echo
    echo "==========================================================="
    echo "This copy has no archive yet, so there is nothing to search."
    echo
    echo "The app is shipped WITHOUT content -- the guidance archive"
    echo "stays with the organisation that maintains it."
    echo
    echo "Ask whoever gave you this app for a copy of its 'data'"
    echo "folder, and put it beside this file. Then open this again."
    echo
    echo "If you ARE the person who maintains it:"
    echo "  export PAKPATAT_ARCHIVE=/path/to/your/archive"
    echo "  .venv/bin/python pipeline/refresh.py bootstrap   # live site"
    echo "  .venv/bin/python pipeline/build_corpus.py"
    echo "  .venv/bin/python build_index.py"
    echo "==========================================================="
    echo
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
  fi
  echo
  echo "Building the offline search index (one time, downloads ~220MB)..."
  python build_index.py
fi

# --- 5. Settings file -----------------------------------------------------
if [ ! -f ".env" ]; then
  cp .env.example .env
  chmod 600 .env          # keys stay readable only by this user
fi

# --- 6. The local engine --------------------------------------------------
#
# The default provider is Ollama, not Gemini (see pakpatat/config.py), so the
# old note here -- "paste a Google key or you get no answers" -- described a
# default this app has not had for some time and sent people to get a key they
# do not need. Windows already said the right thing; this now matches it.
#
# app.py starts the Ollama SERVER on its own, but deliberately never downloads
# MODELS: that is gigabytes, so it stays the user's choice. Without this block
# the first question comes back as "run 'ollama pull ...' in a terminal", which
# is exactly the instruction a non-technical case worker cannot act on.
MODEL="${ASSISTANT_MODEL:-qwen2.5:3b-instruct}"
if command -v ollama >/dev/null 2>&1; then
  # Both `ollama list` and `ollama pull` talk to the server, and a Homebrew
  # install has no menu-bar app to start it -- so bring it up here rather than
  # reporting "no model" when the truth is "nothing was listening".
  if ! curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    ollama serve >/dev/null 2>&1 &
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  if ! ollama list 2>/dev/null | grep -q "^${MODEL%%:*}"; then
    echo
    echo "-----------------------------------------------------------"
    echo "The offline answering model ('$MODEL') is not downloaded yet."
    echo "It is about 2GB, downloaded once, and after that this app"
    echo "answers with no internet and no account -- questions never"
    echo "leave this computer."
    echo "-----------------------------------------------------------"
    printf "Download it now? [y/N] "
    read -r reply
    case "$reply" in
      [Yy]*) ollama pull "$MODEL" ;;
      *) echo "Skipped. Searching the archive still works, and you can"
         echo "choose an online provider in the app's Settings panel." ;;
    esac
  fi
else
  echo
  echo "-----------------------------------------------------------"
  echo "To keep every question on this computer, install Ollama from"
  echo "https://ollama.com/download -- the app starts it for you after"
  echo "that. Otherwise open Settings inside the app and choose an"
  echo "online provider."
  echo "-----------------------------------------------------------"
fi

# --- 7. Confirm this computer can actually answer -------------------------
#
# Five things have to be present for an offline answer, and four of them used
# to fail silently: the window opened looking healthy and only said "that is
# not in the archive" when a real question arrived. Print the same checks the
# splash shows, so a problem is visible here too -- this terminal is where the
# fix commands can actually be run.
echo
python -m pakpatat.preflight || true

echo
echo "Starting Päkpätät..."
python app.py
