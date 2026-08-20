# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
Central configuration for Päkpätät.

Deliberately PROVIDER-AGNOSTIC: the answering model is chosen entirely by two
environment variables, so the CBO can switch between Google Gemini, Anthropic
Claude, OpenAI, or a fully offline local model (Ollama) without touching code.

Default is a LOCAL Ollama model -- no key, no cost, and no community question
leaves the machine. (This docstring said "Gemini Flash" long after the default
below had moved to ollama; a stale default in the one file people read to learn
the defaults is worse than no note at all.)

Retrieval/search is ALWAYS local and offline (no API key, no network), so the
archive stays searchable even with no internet and no credit.
"""
import os
import pathlib
import sys

from dotenv import load_dotenv

# TWO ROOTS, because an installed app has two different kinds of location and
# conflating them is what makes a frozen build fail in ways that look random.
#
#   BUNDLE  read-only program files -- ui/, ui/brand/, anything shipped.
#           Under PyInstaller these live in sys._MEIPASS, NOT next to the .py.
#   HOME    writable state -- data/, .env, the model cache.
#           An app installed to C:\Program Files cannot write there, so a
#           frozen build keeps this in the user's own profile instead. Running
#           from a checkout keeps both at the repository root, unchanged.
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    BUNDLE = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).parent))
    if sys.platform == "win32":
        _state = pathlib.Path(os.getenv("LOCALAPPDATA", pathlib.Path.home())) / "Pakpatat"
    elif sys.platform == "darwin":
        _state = pathlib.Path.home() / "Library" / "Application Support" / "Pakpatat"
    else:
        _state = pathlib.Path(
            os.getenv("XDG_DATA_HOME", pathlib.Path.home() / ".local" / "share")) / "pakpatat"
    HOME = _state
    HOME.mkdir(parents=True, exist_ok=True)
else:
    BUNDLE = HOME = pathlib.Path(__file__).resolve().parents[1]   # repository root

load_dotenv(HOME / ".env")

# ---------------------------------------------------------------- data sources
#
# Nothing here ships with the repository. `data/` is generated on the operator's
# own machine from their own copy of the source material -- see NOTICE.md for
# why the archive itself is not distributed.
#
#   PAKPATAT_DATA     where the corpus and index live      (default: ./data)
#   PAKPATAT_ARCHIVE  the scraped source pages, read ONLY by
#                      pipeline/build_corpus.py             (no default -- it is
#                      whatever the operator has locally)
DATA_DIR = pathlib.Path(os.getenv("PAKPATAT_DATA", HOME / "data")).expanduser()
ARCHIVE_ROOT = (pathlib.Path(os.environ["PAKPATAT_ARCHIVE"]).expanduser()
                if os.getenv("PAKPATAT_ARCHIVE") else None)

# FIRST RUN OF AN INSTALLED BUILD: seed the writable data directory from
# whatever the installer shipped.
#
# The build can carry an archive (packaging/pakpatat.spec, opt-in) into
# BUNDLE/archive. That location is read-only -- Program Files -- so the app
# copies it once into HOME rather than reading it in place, which also means a
# later refresh can rewrite it like any other install.
#
# Copied, not linked, and only when the destination is genuinely absent: an
# operator who has already promoted a newer corpus must never have it silently
# replaced by the one frozen into the installer months earlier.
if FROZEN and (BUNDLE / "archive").is_dir() and not DATA_DIR.exists():
    import shutil
    try:
        shutil.copytree(BUNDLE / "archive", DATA_DIR)
    except OSError:
        pass          # unwritable profile is reported properly by preflight.py

CORPUS = pathlib.Path(os.getenv("PAKPATAT_CORPUS", DATA_DIR / "corpus.jsonl"))
INDEX_DIR = DATA_DIR / "index"
INDEX_VECTORS = INDEX_DIR / "vectors.npy"
INDEX_META = INDEX_DIR / "meta.json"

# ---------------------------------------------------------------- answer model
# DEFAULT IS FULLY OFFLINE: a local Ollama model. No API key, no internet, no
# cost, and no community question ever leaves the machine -- which matters for
# a service handling refugee questions.
#
# Staff can switch to a cloud provider in the app's Settings panel; that writes
# the choice + key to .env (git-ignored, never in the repo).
#
# Anything langchain's init_chat_model understands works:
#   ollama       / qwen2.5:3b-instruct   (default -- offline, free, private)
#   google_genai / gemini-2.5-flash      (cheap, fast, needs key)
#   anthropic    / claude-haiku-4-5
#   openai       / gpt-4o-mini
#
# 3b, not 7b: measured on the reference M1 (16GB, under memory pressure), 7b's
# prompt PREFILL alone was 50.7s of a 53.8s answer -- long enough that the
# desktop webview's ~60s idle timeout read it as "service unreachable" (the
# bug that led to /ask streaming progress at all, see app.py). 3b answers the
# same gold-set questions in ~5-11s. The trade is real -- 3b is weaker at
# grounded citation work than 7b -- but an answer that arrives is worth more
# than a better one the webview kills before it's shown. Staff on a machine
# with headroom can set ASSISTANT_MODEL=qwen2.5:7b-instruct.
MODEL_PROVIDER = os.getenv("ASSISTANT_MODEL_PROVIDER", "ollama")
MODEL_NAME = os.getenv("ASSISTANT_MODEL", "qwen2.5:3b-instruct")
MODEL_TEMPERATURE = float(os.getenv("ASSISTANT_TEMPERATURE", "0"))

# Context window for LOCAL models only (cloud models size their own).
#
# Ollama defaults to 4096 and, crucially, TRUNCATES SILENTLY past it -- no
# error, just an answer written from a prompt with pieces missing. Measured
# prompt sizes on this corpus (8 chunks + system prompt) run 2.3k-4.3k tokens:
#     "How can I tell if someone is a scammer..."  ~4.3k  -> OVER 4096
#     "How do I register with UNHCR?"              ~4.1k  -> OVER 4096
# So the default was already dropping source text on common questions, and a
# dropped chunk is exactly how a phone number goes missing or wrong. 8192 fits
# the worst case with room to spare; the extra KV cache costs ~0.5GB of RAM.
NUM_CTX = int(os.getenv("ASSISTANT_NUM_CTX", "8192"))

# How long Ollama keeps the weights in RAM after a question. The default is 5
# minutes; on a machine where loading a 7B model is slow, an occasional user
# would pay that reload on nearly every question. "-1" = stay loaded.
OLLAMA_KEEP_ALIVE = os.getenv("ASSISTANT_OLLAMA_KEEP_ALIVE", "30m")

# Which env var holds the key, per provider -- used only to give the user a
# clear error message instead of a stack trace.
PROVIDER_KEY_ENV = {
    "google_genai": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "ollama": None,  # local, no key needed
}

# ------------------------------------------------------------------ retrieval
# Local ONNX embedding model. Multilingual: a Burmese question retrieves the
# English UNHCR text (measured ~0.77 cosine similarity on a test pair).
EMBED_MODEL = os.getenv("ASSISTANT_EMBED_MODEL",
                        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
TOP_K = int(os.getenv("ASSISTANT_TOP_K", "8"))

# WHERE THE EMBEDDING MODEL IS CACHED -- and why this is not a detail.
#
# fastembed's default cache is os.path.join(tempfile.gettempdir(),
# "fastembed_cache") -- see .venv/.../fastembed/common/utils.py:53. On macOS
# that resolves inside /var/folders/.../T/, which the OS purges on its own
# schedule; on Linux it is /tmp, cleared on reboot on many distros.
#
# So the 220MB model this app needs to embed EVERY question was living somewhere
# the operating system is entitled to delete. The failure that produces is the
# quiet kind: the archive and index are both fine, the window opens normally,
# and the first question either stalls on a surprise 220MB download or fails
# outright on a laptop with no internet -- which is the machine this app was
# built for.
#
# Pin it next to the data it serves instead. fastembed reads FASTEMBED_CACHE_PATH
# (same file, line 54), so setting it here covers retrieve.py and index.py alike
# without either of them having to pass a cache_dir.
EMBED_CACHE = pathlib.Path(
    os.getenv("FASTEMBED_CACHE_PATH", DATA_DIR / ".models")).expanduser()

# An installed build ships the model inside BUNDLE/models (see the spec). Point
# fastembed straight at it rather than copying 220MB into the user profile --
# it is read-only, which is all a cache needs to be, and it means a fresh
# install can answer its first question with the network off. Only used when
# the user profile has no cache of its own, so a later re-download still wins.
if FROZEN and not (EMBED_CACHE.exists() and any(EMBED_CACHE.glob("models--*"))):
    _bundled = BUNDLE / "models"
    if _bundled.is_dir() and any(_bundled.glob("models--*")):
        EMBED_CACHE = _bundled

os.environ.setdefault("FASTEMBED_CACHE_PATH", str(EMBED_CACHE))

# WHERE A SAVED IMAGE GOES.
#
# The Downloads folder, because that is where a person looks for something an
# app has just given them. The first version of the share-image feature wrote
# into data/posts/ inside the application folder -- somewhere no case worker
# would think to open, and which reinstalling would wipe -- while the
# confirmation message said "saved to your Desktop". Wrong place, and a wrong
# message about the wrong place.
DOWNLOADS_DIR = pathlib.Path(
    os.getenv("PAKPATAT_DOWNLOADS", pathlib.Path.home() / "Downloads")
).expanduser()

# The OS temp location fastembed would have used. preflight.py checks it so it
# can tell the difference between "not downloaded" and "downloaded somewhere
# temporary", which need different advice.
import tempfile  # noqa: E402
LEGACY_EMBED_CACHE = pathlib.Path(tempfile.gettempdir()) / "fastembed_cache"

# --- The refusal gate, and an honest account of what it can and cannot do ---
#
# Measured on RAW cosine similarity (absolute, comparable across queries), NOT
# the fused ranking score -- the fused score is min-max normalised, so the best
# chunk always scores ~1.0 even when every chunk is irrelevant.
#
# Measurements on this corpus show the in-scope and out-of-scope score
# distributions OVERLAP, so no threshold separates them cleanly:
#     "REMEDI price"           (in scope)  dense 0.325
#     "book a flight to Bangkok" (out)     dense 0.529
# Set the gate high enough to catch the second and it refuses the first --
# turning a person away from information the archive genuinely contains, which
# is the worse failure for a service like this.
#
# So the gate is deliberately LOW and does one job: reject obvious nonsense
# ("how to make a pizza" 0.18, "best football team" 0.12) cheaply, without
# calling the model. Borderline questions are allowed through and handled by
# the two layers that actually do the heavy lifting:
#   - the model only ever sees retrieved archive text and is told to answer
#     solely from it (pakpatat/graph.py SYSTEM_PROMPT)
#   - every citation it emits is verified in code against what was retrieved
#     (pakpatat/graph.py node_verify)
MIN_SCORE = float(os.getenv("ASSISTANT_MIN_SCORE", "0.28"))

# NOTE: an earlier version raised the gate for non-Latin (e.g. Burmese)
# queries. Measurement killed that idea -- for Burmese the score carries almost
# no relevance signal:
#     "REMEDI cost"          (in scope, top hit = the REMEDI page)  0.383
#     "I want to go hospital"(in scope, top hit = NGO Clinics)      0.424
#     "how to cook chicken"  (off topic)                            0.552
# Relevant questions scored LOWER than irrelevant ones, so any threshold high
# enough to block the cooking question also refused two real questions whose
# top hit was the correct page. One flat, permissive gate it is; off-topic
# non-English questions are caught by the model's grounding instead.

# Answers whose best source is below this are still produced, but flagged to
# the reader as a weak match worth double-checking. Set just under the median
# score of realistic community questions (~0.64) so the flag stays meaningful:
# warn often enough to be useful, rarely enough that staff don't learn to
# ignore it.
WEAK_SCORE = float(os.getenv("ASSISTANT_WEAK_SCORE", "0.50"))

# --- Preferring current guidance over stale guidance, in the ranking itself ---
#
# Relevance is not the only axis that matters in this archive: two chunks can be
# equally on-topic while one has been replaced. These multiply a chunk's fused
# ranking score so that stale material loses TIES to current material without
# disappearing.
#
# This USED to be one flat multiplier over all 262 retired-site chunks, kept
# deliberately mild because demoting them steeply would trade a staleness
# problem for a recall problem -- the retired capture holds whole topics the new
# site dropped, and of the two failures the missing answer is worse, because a
# stale one is at least flagged to the reader.
#
# That tension was real but it was a false choice, and the data to dissolve it
# was already on every chunk. gap_analysis.json records, per retired page,
# whether the live site still covers it. Two populations hide inside that one
# multiplier:
#
#   182 chunks the live site HAS an equivalent for (kept/renamed/merged/
#       restructured). Here the live page is simply the better answer, and a
#       mild 0.90 was too weak to reliably say so.
#    80 chunks the live site has NOTHING for (dropped/downgraded) -- the NGO
#       clinic directory, Verify Plus, the refugee lexicon. refugeemalaysia.org
#       was taken down on 2026-07-14, so this is the only copy that exists.
#       Demoting these at all is indefensible: there is no fresher version to
#       prefer, only this or nothing.
#
# So the rule is now "live first, archive only where live is silent", enforced
# per topic rather than per source. Measure any change to these with
# eval/eval_retrieval.py --compare before shipping it.
#
# Retired material the live site still covers. Loses to the live page on any
# question they both answer; still retrievable when nothing current matches.
# 0.88, and that is a measured ceiling rather than a taste. Swept against
# eval/eval_retrieval.py: 0.90 and 0.88 hold recall@8 at 90%; 0.85 and 0.80
# both drop it to 80%, losing the Burmese "how much does REMEDI cover per day"
# question entirely (the RM160 fact falls from rank 5 to 17). The cause is
# worth recording, because it is the limit of this whole mechanism: the live
# site HAS a REMEDI page, so gap_analysis marks the retired one "kept" -- but
# the retired page carries that fact in 6 chunks and the live page in 1. A
# page-level equivalence is not a fact-level one, so pushing "kept" material
# harder than this starts deleting answers the live site does not actually
# replace.
SUPERSEDED_BY_LIVE_WEIGHT = float(
    os.getenv("ASSISTANT_SUPERSEDED_BY_LIVE_WEIGHT", "0.88"))

# Retired material with no live counterpart. NOT demoted -- 1.0 -- because
# there is nothing current to prefer over it. Exposed as a setting so the
# choice is visible and arguable, not so it should be lowered.
GAP_WEIGHT = float(os.getenv("ASSISTANT_GAP_WEIGHT", "1.0"))

# Retired pages gap_analysis.json could not classify, and the fallback whenever
# no gap analysis is present at all (a fresh install that crawled only the live
# site has none). Unchanged at the old flat value: "we do not know whether the
# live site covers this" is exactly the case the mild demotion was written for.
STALE_WEIGHT = float(os.getenv("ASSISTANT_STALE_WEIGHT", "0.90"))

# Which gap_analysis statuses mean "the live site carries this topic now".
# Kept here beside the weights because the two only make sense together.
LIVE_HAS_EQUIVALENT = frozenset({"kept", "renamed", "merged", "restructured"})
LIVE_HAS_NOTHING = frozenset({"dropped", "downgraded"})

# Harder push for chunks a newer source is KNOWN to contradict (build_corpus.py
# marks these from 07_partner_materials/_index.json). Not zero: the reader may
# still need to see what the old answer said, and format_sources labels it
# "SUPERSEDED" so the model is told not to present it as current.
SUPERSEDED_WEIGHT = float(os.getenv("ASSISTANT_SUPERSEDED_WEIGHT", "0.60"))

# Weight of dense (semantic) vs sparse (keyword/BM25) in the hybrid ranking.
#
# Tuned on 8 factual lookups (hotline numbers, emails, fees, addresses) -- the
# queries where being wrong causes real harm. At 0.65 the registration-hotline
# chunk ranked #7 and fell outside the window, so the assistant wrongly said
# "not in the archive". At 0.50 it ranks #5 and 7 of 8 facts rank in the top 5.
# Embeddings are weak at exact-token recall (that same chunk ranked #49 on
# dense alone), so BM25 needs equal say.
DENSE_WEIGHT = float(os.getenv("ASSISTANT_DENSE_WEIGHT", "0.5"))

# Reciprocal Rank Fusion constant. Fusion combines RANKS, not scores.
#
# The previous approach min-max normalised each score list and added them. That
# is fragile whenever the two distributions have different shapes, and measured
# on eval_retrieval.py it was losing real answers: a Burmese question for the
# registration hotline had the correct chunk at BM25 rank 4, but min-max fusion
# pushed it to rank 15 -- outside TOP_K, so the app said "not in the archive"
# for a number the archive plainly holds. RRF put it back at rank 4.
#
# Larger K flattens the contribution of top ranks (less swing from either
# method); 60 is the standard value from the original RRF paper and measured no
# worse than 20 on this corpus.
RRF_K = float(os.getenv("ASSISTANT_RRF_K", "60"))


def key_missing() -> str | None:
    """Return the name of the env var the user still needs to set, or None."""
    var = PROVIDER_KEY_ENV.get(MODEL_PROVIDER, None)
    if var and not os.getenv(var):
        return var
    return None
