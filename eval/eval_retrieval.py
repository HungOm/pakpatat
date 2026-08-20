#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
Retrieval benchmark for the archive assistant.

WHY THIS EXISTS
---------------
The tuned constants in pakpatat/config.py (DENSE_WEIGHT=0.5, TOP_K=8, MIN_SCORE=0.28)
were each set by measurement after an initial guess proved wrong -- but the
measurements themselves were never kept, so any later change to chunking,
embeddings or fusion weights was a blind edit against numbers nobody could
re-check. This file is that missing check.

WHAT IT MEASURES
----------------
Not "does the answer look good" -- that needs a human. It measures the one
thing that must be true before a good answer is even possible:

    Did the exact fact land in the window of chunks the model gets to see?

If the hotline number is not in the retrieved text, the model cannot produce
it. It can only refuse (good) or invent one (catastrophic). So every gold case
pairs a realistic question with a literal fact string -- a phone number, an
email, a fee -- that MUST appear in the top-K chunks.

It also reports rank under dense-only and BM25-only scoring. That split is what
originally justified DENSE_WEIGHT=0.5: the registration hotline chunk ranked
#49 on dense alone and #1 on BM25. If a future change quietly re-breaks exact
-token recall, the per-method ranks show it immediately.

Finally it reports prompt size, because prompt tokens are the app's real
bottleneck: on the reference M1, prefill was 50.7s of a 53.8s answer.

USAGE
-----
    python eval_retrieval.py                     # run, print a report
    python eval_retrieval.py --save baseline.json    # record today's numbers
    python eval_retrieval.py --compare baseline.json # diff vs a saved run
                                                     # exits 1 on regression
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# Runnable directly (`python eval/eval_retrieval.py`) from a clean checkout,
# without requiring an editable install first.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pakpatat import config, retrieve  # noqa: E402

# --------------------------------------------------------------- the gold set
#
# Every `fact` below was verified to exist verbatim in corpus.jsonl before
# being written here. A case failing therefore means retrieval regressed, NOT
# that the archive lacks the answer -- which is exactly the signal we want.
#
# kind="phone" compares by digits only, because the corpus writes the same
# number several ways ("+603 4288 6364", "+60342886364", "0176143810").
GOLD = [
    # -- UNHCR's own contact routes. Sending someone to the wrong inbox stalls
    #    a case for months, so these are the highest-stakes lookups here.
    dict(q="What email do I use if I have trouble logging in to My Services?",
         fact="mlslureg@unhcr.org", kind="email"),
    dict(q="How do I report someone asking me for money for UNHCR services?",
         fact="mlslufrd@unhcr.org", kind="email"),
    dict(q="How can I appeal my rejected RSD application?",
         fact="mlslursd@unhcr.org", kind="email"),

    # -- The two hotlines. The registration one is the case that forced
    #    DENSE_WEIGHT down to 0.5; keep it here permanently as a tripwire.
    dict(q="What is the UNHCR registration hotline number?",
         fact="0176143810", kind="phone"),
    dict(q="Who do I call if someone has been arrested or detained?",
         fact="+60126305060", kind="phone"),

    # -- Money. A wrong fee sends someone to a clinic they cannot pay for.
    dict(q="How much does REMEDI cover per day for a hospital room?",
         fact="RM160", kind="fee"),

    # -- Document formats: people copy these into forms character by character.
    dict(q="What format is the UNHCR group number written in?",
         fact="123-45678901", kind="text"),

    # -- A clinic phone number, written differently in two chunks. Catches
    #    over-eager normalisation in the chunker as much as in retrieval.
    dict(q="What is the phone number for the MERCY clinic in Ampang?",
         fact="+60342886364", kind="phone"),

    # -- Cross-lingual. A Burmese question must still retrieve the English
    #    page holding the fact; pakpatat/config.py notes Burmese scores carry little
    #    relevance signal, so this is the case most likely to rot silently.
    dict(q="REMEDI က ဆေးရုံတက်ရင် တစ်ရက်ကို ဘယ်လောက် ကျခံပေးမလဲ။",
         fact="RM160", kind="fee"),
    dict(q="UNHCR မှာ မှတ်ပုံတင်ဖို့ ဖုန်းနံပါတ် ဘယ်လောက်လဲ။",
         fact="0176143810", kind="phone"),
]

# Questions the archive genuinely cannot answer. These do NOT assert a
# pass/fail -- pakpatat/config.py documents that in-scope and out-of-scope scores
# overlap, so no threshold separates them cleanly. They are printed so that a
# change which suddenly makes nonsense score high is visible to a human.
OFF_TOPIC = [
    "How do I book a flight to Bangkok?",
    "What is the best football team in the world?",
    "How do I cook chicken curry?",
]


def _norm(fact: str, kind: str) -> str:
    """Canonical form for comparison. Phones lose their formatting and country
    prefix (0176143810 and +60176143810 are the same line); everything else is
    matched case-insensitively with whitespace collapsed."""
    if kind == "phone":
        digits = re.sub(r"\D", "", fact)
        return digits[-9:]                      # drop 0/60 prefix, keep the line
    return re.sub(r"\s+", " ", fact).strip().lower()


def _contains(haystack: str, fact: str, kind: str) -> bool:
    if kind == "phone":
        return _norm(fact, kind) in re.sub(r"\D", "", haystack)
    return _norm(fact, kind) in re.sub(r"\s+", " ", haystack).lower()


def _rank_of(results: list[dict], fact: str, kind: str) -> int | None:
    """1-based rank of the first chunk containing the fact, or None."""
    for i, r in enumerate(results, 1):
        if _contains(r["text"], fact, kind):
            return i
    return None


def _single_method_ranks(query: str, fact: str, kind: str) -> tuple[int | None, int | None]:
    """Rank under dense-only and BM25-only scoring.

    Diagnostic only -- it reaches into retrieve's loaded state rather than
    going through search(), because search() only exposes the fused order.
    """
    import numpy as np

    st = retrieve._load()
    chunks = st["chunks"]

    q_vec = np.array(list(st["embedder"].embed([query]))[0], dtype=np.float32)
    q_vec /= np.linalg.norm(q_vec)
    dense = st["vectors"] @ q_vec
    sparse = np.array(st["bm25"].get_scores(retrieve._tokenize(query)), dtype=np.float32)

    out = []
    for scores in (dense, sparse):
        rank = None
        for pos, idx in enumerate(np.argsort(-scores), 1):
            if _contains(chunks[idx]["text"], fact, kind):
                rank = pos
                break
        out.append(rank)
    return out[0], out[1]


def run() -> dict:
    st = retrieve._load()
    n_chunks = len(st["chunks"])
    top_k = config.TOP_K

    cases = []
    for g in GOLD:
        full = retrieve.search(g["q"], top_k=n_chunks)     # production ordering
        window = full[:top_k]                              # what the model sees

        prompt = retrieve.format_sources(window)
        d_rank, b_rank = _single_method_ranks(g["q"], g["fact"], g["kind"])

        cases.append({
            "q": g["q"],
            "fact": g["fact"],
            "kind": g["kind"],
            "hit": _rank_of(window, g["fact"], g["kind"]) is not None,
            "rank_fused": _rank_of(full, g["fact"], g["kind"]),
            "rank_dense": d_rank,
            "rank_bm25": b_rank,
            "top_raw": window[0]["raw"] if window else 0.0,
            "prompt_chars": len(prompt),
            # Rough: this corpus averages ~3.6 chars/token under the qwen
            # tokenizer. Good enough to track a trend, not a billing figure.
            "prompt_tokens_est": round(len(prompt) / 3.6),
        })

    off = []
    for q in OFF_TOPIC:
        r = retrieve.search(q, top_k=1)
        off.append({"q": q, "top_raw": r[0]["raw"] if r else 0.0,
                    "gated": (r[0]["raw"] if r else 0.0) < config.MIN_SCORE})

    hits = sum(c["hit"] for c in cases)
    toks = [c["prompt_tokens_est"] for c in cases]
    return {
        "config": {
            "top_k": top_k, "dense_weight": config.DENSE_WEIGHT,
            "min_score": config.MIN_SCORE, "embed_model": config.EMBED_MODEL,
            "chunks": n_chunks,
        },
        "summary": {
            "cases": len(cases),
            "hits": hits,
            "recall_at_k": round(hits / len(cases), 4),
            "prompt_tokens_mean": round(sum(toks) / len(toks)),
            "prompt_tokens_max": max(toks),
        },
        "cases": cases,
        "off_topic": off,
    }


def _fmt_rank(r: int | None) -> str:
    return "--" if r is None else str(r)


def report(res: dict) -> None:
    c = res["config"]
    print(f"\nchunks={c['chunks']}  TOP_K={c['top_k']}  "
          f"DENSE_WEIGHT={c['dense_weight']}  MIN_SCORE={c['min_score']}")
    print(f"{'':2} {'hit':>4} {'fused':>6} {'dense':>6} {'bm25':>6} {'tok':>6}  question")
    print("-" * 92)
    for i, x in enumerate(res["cases"], 1):
        flag = "ok " if x["hit"] else "MISS"
        # A fact ranked well past the window on one method but fine on the
        # other is the fusion earning its keep -- worth seeing at a glance.
        print(f"{i:2} {flag:>4} {_fmt_rank(x['rank_fused']):>6} "
              f"{_fmt_rank(x['rank_dense']):>6} {_fmt_rank(x['rank_bm25']):>6} "
              f"{x['prompt_tokens_est']:>6}  {x['q'][:44]}")

    s = res["summary"]
    print("-" * 92)
    print(f"recall@{c['top_k']} = {s['hits']}/{s['cases']} ({s['recall_at_k']:.0%})   "
          f"prompt tokens: mean {s['prompt_tokens_mean']}, max {s['prompt_tokens_max']}")

    print("\noff-topic (informational -- scores overlap with real questions "
          "by design, see config.MIN_SCORE):")
    for o in res["off_topic"]:
        print(f"   raw={o['top_raw']:.3f}  {'gated' if o['gated'] else 'PASSES gate'}"
              f"   {o['q']}")


def compare(res: dict, path: pathlib.Path) -> int:
    """Diff against a saved baseline. Exit code 1 on any regression, so this
    can gate a change instead of merely describing it."""
    old = json.loads(path.read_text(encoding="utf-8"))
    by_q = {c["q"]: c for c in old["cases"]}

    # What counts as a regression: a fact that USED to reach the model and now
    # doesn't. Rank shuffling inside the window is not a regression -- the
    # model sees all TOP_K chunks regardless of their order within it, and
    # flagging those trains the reader to ignore this report (the same trap
    # config.WEAK_SCORE warns about for user-facing warnings).
    regressions, improvements, moves = [], [], []
    for new in res["cases"]:
        prev = by_q.get(new["q"])
        if not prev:
            continue
        if prev["hit"] and not new["hit"]:
            regressions.append(
                f"LOST  {new['fact']} (rank {prev['rank_fused']}->"
                f"{new['rank_fused']}) -- {new['q'][:44]}")
        elif not prev["hit"] and new["hit"]:
            improvements.append(
                f"FOUND {new['fact']} (rank {prev['rank_fused']}->"
                f"{new['rank_fused']}) -- {new['q'][:44]}")
        elif new["hit"] and prev["rank_fused"] and new["rank_fused"]:
            d = new["rank_fused"] - prev["rank_fused"]
            if abs(d) >= 3:
                # Inside the window, so harmless -- but a big move is worth
                # seeing, because it means the ranking shifted under you.
                moves.append(f"rank {prev['rank_fused']}->{new['rank_fused']} "
                             f"(still in top {res['config']['top_k']}) -- {new['q'][:40]}")

    o, n = old["summary"], res["summary"]
    print(f"\nrecall@k   {o['recall_at_k']:.0%} -> {n['recall_at_k']:.0%}")
    print(f"mean tokens {o['prompt_tokens_mean']} -> {n['prompt_tokens_mean']} "
          f"({n['prompt_tokens_mean'] - o['prompt_tokens_mean']:+d})")
    for line in improvements:
        print("  + " + line)
    for line in moves:
        print("  ~ " + line)
    for line in regressions:
        print("  ! " + line)
    if regressions:
        print(f"\n{len(regressions)} regression(s).")
        return 1
    print("\nNo regressions.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", metavar="FILE")
    ap.add_argument("--compare", metavar="FILE")
    a = ap.parse_args()

    res = run()
    report(res)

    if a.save:
        pathlib.Path(a.save).write_text(
            json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nsaved -> {a.save}")
    if a.compare:
        return compare(res, pathlib.Path(a.compare))
    return 0


if __name__ == "__main__":
    sys.exit(main())
