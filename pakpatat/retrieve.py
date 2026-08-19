"""
Hybrid retrieval over the archive: dense (multilingual semantic) + sparse
(BM25 keyword), score-fused.

Why hybrid rather than pure semantic:
  - Semantic catches paraphrase and CROSS-LANGUAGE hits (a Burmese question
    retrieves the English UNHCR page).
  - BM25 catches the exact tokens that matter most in this domain and that
    embeddings routinely blur: hotline numbers, "REMEDI", "A3Z-", clinic names,
    "DPP". Losing those would be the difference between a usable referral and
    a wrong phone number.

Everything here is local and offline -- no API key, no network.
"""
import json
import re

import numpy as np

from . import config

_STATE: dict = {}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9က-႟]+", text.lower())


def _currency_weight(chunk: dict) -> float:
    """Ranking multiplier for how current a chunk is (1.0 = no change).

    Deliberately mild. The retired site is not wrong by default -- it holds 262
    chunks including topics the new site dropped, and demoting it steeply would
    trade a staleness problem for a recall problem, which is the worse of the
    two: a stale answer is flagged to the reader, a missing one is not.
    """
    if (chunk.get("status") or "").lower() == "superseded":
        return config.SUPERSEDED_WEIGHT
    if chunk["source"] == "old_site_refugeemalaysia_org":
        return config.STALE_WEIGHT
    return 1.0


def reset() -> None:
    """Drop the cached index so the next question reloads it from disk.

    _load() caches for the life of the process, which is right for a tool that
    answers hundreds of questions from a fixed archive -- but it means a
    rebuilt index is invisible to a running app. pipeline/refresh.py works
    around that by telling the operator to quit and reopen; the in-app rebuild
    (pakpatat/firstrun.py) cannot ask a case worker to do that, so it calls
    this instead.

    Only the dict is emptied, never rebound: retrieve's own functions close
    over this module-level name, and rebinding it would leave them reading a
    dict nobody updates.
    """
    _STATE.clear()


def _load():
    """Lazy-load index + models once per process.

    Returns a SNAPSHOT of the cache, not the cache itself. reset() empties
    _STATE when a rebuilt index is swapped in, and a search that had already
    taken the dict would find its keys vanishing underneath it mid-question.
    The copy is five references, not five megabytes.
    """
    if _STATE:
        return dict(_STATE)

    if not config.INDEX_META.exists():
        raise SystemExit(
            "Search index not found.\n"
            "Run this first:   python build_index.py"
        )

    meta = json.loads(config.INDEX_META.read_text(encoding="utf-8"))
    chunks = meta["chunks"]

    from fastembed import TextEmbedding
    from rank_bm25 import BM25Okapi

    _STATE["chunks"] = chunks
    _STATE["vectors"] = np.load(config.INDEX_VECTORS)
    # Precomputed once, not per query: how much each chunk's ranking is nudged
    # by how current it is. See the note in search().
    _STATE["currency_weight"] = np.array(
        [_currency_weight(c) for c in chunks], dtype=np.float32)
    _STATE["embedder"] = TextEmbedding(meta.get("embed_model", config.EMBED_MODEL))
    _STATE["bm25"] = BM25Okapi([
        _tokenize(f"{c['doc_title']} {c.get('section_heading') or ''} {c['text']}")
        for c in chunks
    ])
    return dict(_STATE)


def _minmax(a: np.ndarray) -> np.ndarray:
    """Normalise a score array to 0..1. Used only to present a readable
    `score` in the UI -- fusion itself no longer relies on it (see _rrf)."""
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-9:
        return np.zeros_like(a)
    return (a - lo) / (hi - lo)


def _ranks(scores: np.ndarray) -> np.ndarray:
    """1-based rank of every element, best score = rank 1."""
    order = np.argsort(-scores)
    out = np.empty(len(scores), dtype=np.float32)
    out[order] = np.arange(1, len(scores) + 1, dtype=np.float32)
    return out


def _bm25_weight(tokens: list[str], bm25) -> float:
    """How much say BM25 gets, based on how much of the query it can actually
    see.

    BM25 can only score words that exist in the corpus. A Burmese question puts
    one recognisable token in front of it -- "REMEDI" or "UNHCR" -- and scores
    every chunk on the page equally by it, which is noise wearing the costume
    of a signal. Measured on eval_retrieval.py, that noise was outvoting a
    correct dense hit: the Burmese REMEDI question had the right chunk at dense
    rank 1, and fusing in an uninformed BM25 pushed it to rank 11, outside the
    window, so the app claimed the archive didn't cover it.

    So BM25's weight scales with vocabulary coverage. English questions measure
    1.00 coverage and are therefore completely unaffected -- this can only
    change queries BM25 was never able to read. That is deliberate: the English
    ranking was tuned by measurement and should not move.

    NOTE: this is NOT the script-aware rule that config.MIN_SCORE warns was
    tried and rejected. That one gated REFUSAL on non-Latin script and refused
    real questions. This weighs ORDERING by a measurable property of the query
    (are its words in the index at all), never blocks an answer, and is checked
    by the gold set on every run.
    """
    if not tokens:
        return 1 - config.DENSE_WEIGHT
    coverage = sum(1 for t in tokens if t in bm25.idf) / len(tokens)
    dense_w = config.DENSE_WEIGHT + (1 - config.DENSE_WEIGHT) * (1 - coverage)
    return 1 - dense_w


def _rrf(dense: np.ndarray, sparse: np.ndarray, sparse_w: float) -> np.ndarray:
    """Reciprocal Rank Fusion: combine the two rankings by position, not by
    score magnitude.

    Cosine similarity and BM25 are on incomparable scales, and normalising them
    onto a shared 0..1 axis makes the fused result depend on the SHAPE of each
    distribution -- one method having a long tail can bury a chunk the other
    method ranked near the top. Ranks have no such problem: rank 4 means the
    same thing on both sides. See config.RRF_K for the measurement that
    prompted the switch.
    """
    return ((1 - sparse_w) / (config.RRF_K + _ranks(dense))
            + sparse_w / (config.RRF_K + _ranks(sparse)))


def search(query: str, top_k: int | None = None,
           source_filter: str | None = None) -> list[dict]:
    """Return the top_k most relevant chunks, each with a fused `score` (0..1).

    source_filter: None (both archives), 'new' (live help.unhcr.org only),
    or 'old' (retired refugeemalaysia.org only).
    """
    st = _load()
    top_k = top_k or config.TOP_K
    chunks = st["chunks"]

    q_tokens = _tokenize(query)
    q_vec = np.array(list(st["embedder"].embed([query]))[0], dtype=np.float32)
    q_vec /= np.linalg.norm(q_vec)
    dense = st["vectors"] @ q_vec                       # cosine, already unit-norm
    sparse = np.array(st["bm25"].get_scores(q_tokens), dtype=np.float32)

    # `fused` ranks results against each other. The best chunk always scores
    # ~1.0 even when everything is irrelevant -- useful for ORDERING, useless
    # for deciding "do we know this at all".
    fused = _minmax(_rrf(dense, sparse,        # min-max here is for display only
                         _bm25_weight(q_tokens, st["bm25"])))

    # PREFER WHAT IS CURRENT.
    #
    # Relevance alone is not the whole ranking question here: two chunks can be
    # equally on-topic while one of them is guidance UNHCR has since replaced.
    # Retrieval used to be blind to that, so a retired-site page could outrank
    # the live answer and the model would lead with it.
    #
    # This is a nudge, not a filter. Stale material stays reachable -- the
    # retired site still holds 262 chunks of guidance the new site dropped
    # entirely, and burying it would lose real answers. It only has to lose
    # TIES against something current. Superseded chunks are pushed harder,
    # because for those a newer source is known to contradict them.
    #
    # ...but ONLY when the query actually matched something. Measured on
    # "How much are school fees for refugee children?" (raw 0.463, a weak
    # match): with the weighting applied, REMEDI chunks climbed to ranks 4-6
    # and pushed Education chunks down; with it off they sat at 6-8, where they
    # belong. The REMEDI child material is lexically dense in "children" and
    # "RM", so on a question the archive cannot really answer it looks
    # competitive -- and a blanket currency boost then promotes an insurance
    # page over the topic actually asked about.
    #
    # This weighting exists to break near-ties between sources that are ALL
    # relevant. When nothing is clearly relevant there are no ties worth
    # breaking, only noise to reorder -- so leave the ranking on pure relevance
    # and let the weak-match warning do its job.
    # MEASURED, because it was suspected of something it does not do.
    #
    # "How much are school fees for refugee children?" returns three REMEDI
    # chunks in its top 8, which looks like this weighting promoting insurance
    # material over education. Swept from 0.90 to 1.00 (i.e. off entirely) the
    # count does not move: REMEDI=3, Education=5 at every value. The weighting
    # shifts those chunks between ranks 6-8 and 4-6 and changes nothing about
    # WHICH chunks are retrieved.
    #
    # The real cause is lexical: the REMEDI child material is dense in
    # "children", "RM" and "parent or guardian", which is also the vocabulary
    # of a school-fees question. That is a retrieval limit, handled by the
    # weak-match warning and by the regurgitation check in graph.py -- not by
    # tuning this number. Left at the measured value; do not re-tune it against
    # that symptom.
    fused = fused * st["currency_weight"]

    # `raw` is absolute cosine similarity, comparable across queries. This is
    # what the refusal gate in graph.py reads. Keeping the two separate is the
    # difference between rejecting an off-topic question and confidently
    # answering it from the least-irrelevant page.
    order = np.argsort(-fused)
    results = []
    for i in order:
        c = chunks[i]
        # "Latest guidance" must include material UNHCR handed over directly.
        # Filtering on the site name alone excluded partner_materials -- so the
        # one filter a user picks to get the MOST current answer was hiding the
        # newest source in the archive.
        if source_filter == "new" and c["source"] not in (
                "new_site_help_unhcr_org", "partner_materials"):
            continue
        if source_filter == "old" and c["source"] != "old_site_refugeemalaysia_org":
            continue
        results.append({
            **c,
            "score": round(float(fused[i]), 4),
            "raw": round(float(dense[i]), 4),
        })
        if len(results) >= top_k:
            break
    return results


def format_sources(results: list[dict]) -> str:
    """Render retrieved chunks as numbered [S1]..[Sn] blocks for the prompt.

    The numbering is the citation contract: the model may only cite these IDs,
    and pakpatat/graph.py verifies every citation it emits against this exact set.
    """
    blocks = []
    for n, r in enumerate(results, 1):
        # Derive the label from the chunk's own status FIRST, and only fall back
        # to guessing from `source`.
        #
        # This used to be a straight binary -- new site = CURRENT, anything else
        # = RETIRED -- which silently mislabelled two cases the corpus now
        # contains. Material handed over by UNHCR in person was announced to the
        # model as "FROM THE RETIRED OLD SITE - may be out of date", i.e. the
        # freshest source in the archive was the one the model was told to
        # distrust; and a live-site chunk marked superseded by that newer
        # material was still announced as CURRENT. Rule 5 of the system prompt
        # tells the model to prefer whichever source is marked current, so
        # getting these labels wrong actively steers it to the stale answer.
        status = (r.get("status") or "").lower()
        if status == "superseded":
            currency = ("SUPERSEDED - newer official material replaces this. "
                        "Do NOT present it as current")
        elif r["source"] == "partner_materials":
            currency = ("CURRENT - given directly to this organisation by "
                        "UNHCR; newer than the website")
        elif r["source"] == "new_site_help_unhcr_org":
            currency = "CURRENT (live UNHCR site)"
        else:
            currency = "FROM THE RETIRED OLD SITE - may be out of date"
        heading = f" > {r['section_heading']}" if r.get("section_heading") else ""
        # Where a chunk carries a fixed citation (the REMEDI minute), show that
        # instead of a URL. The URL for these points at the closest live page
        # for cross-checking, but it is NOT where the fact came from -- and a
        # model shown only a help.unhcr.org link will attribute the minute's
        # content to the website, which is exactly the wrong attribution when
        # the website still says the opposite.
        # Label deliberately NOT "Source document:". With that wording the model
        # started emitting "[SD1]" instead of "[S1]" -- it blended the label
        # into the citation tag. An invalid tag is worse than a missing one:
        # it looks like a citation to the reader but matches nothing, so the
        # verifier cannot check it and the UI cannot link it.
        origin = (f"Recorded in: {r['citation']}" if r.get("citation")
                  else f"URL: {r['url']}")
        cross = (f"\nCross-check (may not yet reflect this): {r['url']}"
                 if r.get("citation") else "")
        blocks.append(
            f"[S{n}] {r['doc_title']}{heading}\n"
            f"Status: {currency}\n"
            f"{origin}{cross}\n"
            f"---\n{r['text']}"
        )
    return "\n\n".join(blocks)
