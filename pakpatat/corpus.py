# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
Turn an archive of markdown pages into the chunked, metadata-tagged corpus.

This used to live entirely inside pipeline/build_corpus.py, which read its
paths from environment variables at import time and called sys.exit() when
PAKPATAT_ARCHIVE was unset. That was fine for a script and impossible for the
app: an installed build has no `pipeline/` directory, no `python` on PATH and
no PAKPATAT_ARCHIVE, so the corpus could only ever be rebuilt by a developer at
a terminal. Every button that refreshes the archive needs this logic in-process.

So the logic moved here, unchanged, taking paths as ARGUMENTS. The CLI still
exists and still behaves identically -- pipeline/build_corpus.py is now a thin
wrapper that resolves the environment and calls build() below.

The chunking is deliberately byte-for-byte what it was. A drift of even a
newline re-chunks the whole archive, and pipeline/refresh.py's `diff` step --
the human review that catches a changed hotline number -- reads as "every page
rewritten" when that happens, which is the same as no review at all.

Every chunk carries enough metadata to answer, at query time:
  - which site it came from
  - whether it is still current (new site) or superseded/dropped (old site)
  - the source URL, for citing back to UNHCR
  - which UNHCR topics it belongs to, so coverage can be reported honestly
"""
import datetime as dt
import json
import pathlib
import re

# Chunk size is the assistant's main cost driver: 8 chunks go into every
# prompt, and on the reference M1 prompt PREFILL was 50.7s of a 53.8s answer.
# Measured with eval/eval_retrieval.py, the old 350-word budget
# produced prompts averaging 3.4k tokens and peaking at 5.9k.
MAX_CHUNK_WORDS = 220

# Carried from the end of one chunk into the start of the next, so a fact that
# lands on a boundary ("Registration hotline:" / "0176143810") is not orphaned
# from the label that makes it findable. Kept small: overlapping text can cause
# two near-duplicate chunks to occupy two of the eight prompt slots.
OVERLAP_WORDS = 30

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

# Where each source sits inside an archive root. Overridable by the CLI, which
# still honours the per-directory environment variables it always did.
OLD_SUBDIR = "01_support_topics"
NEW_SUBDIR = "04_help_unhcr_2026"
PARTNER_SUBDIR = "07_partner_materials"
GAP_SUBPATH = "05_intelligence/gap_analysis/gap_analysis.json"


# ------------------------------------------------------------------ chunking
def split_sections(text):
    """Split markdown on ## headings; keep the H1 title with the first chunk."""
    parts = re.split(r"\n(?=## )", text)
    sections = []
    for p in parts:
        heading_match = re.match(r"^## (.+)", p.strip())
        heading = heading_match.group(1).strip() if heading_match else None
        sections.append((heading, p.strip()))
    return sections


def _is_table(block):
    return block.lstrip().startswith("|")


def _split_block(block):
    """Break one oversized paragraph on sentence boundaries.

    The old chunker only ever split BETWEEN paragraphs, so a single long
    paragraph could not be split at all -- that is how a 4,030-character chunk
    ended up in the corpus. Tables are exempt: a row torn away from its header
    row is unreadable, and a phone number separated from the clinic name beside
    it is worse than not retrieving it at all.
    """
    if _is_table(block) or len(block.split()) <= MAX_CHUNK_WORDS:
        return [block]
    out, cur, n = [], [], 0
    for sent in _SENTENCE.split(block):
        sw = len(sent.split())
        if n + sw > MAX_CHUNK_WORDS and cur:
            out.append(" ".join(cur))
            cur, n = [], 0
        cur.append(sent)
        n += sw
    if cur:
        out.append(" ".join(cur))
    return out


def _tail(text, words):
    parts = text.split()
    return " ".join(parts[-words:]) if len(parts) > words else text


def split_oversized(heading, text):
    if len(text.split()) <= MAX_CHUNK_WORDS:
        return [(heading, text)]

    blocks = []
    for para in text.split("\n\n"):
        blocks.extend(_split_block(para))

    chunks, cur, n = [], [], 0
    for b in blocks:
        bw = len(b.split())
        if n + bw > MAX_CHUNK_WORDS and cur:
            chunks.append("\n\n".join(cur))
            carry = _tail(cur[-1], OVERLAP_WORDS) if OVERLAP_WORDS else ""
            cur, n = ([carry] if carry else []), len(carry.split())
        cur.append(b)
        n += bw
    if cur:
        chunks.append("\n\n".join(cur))
    return [(heading, c) for c in chunks]


def chunk_doc(text):
    out = []
    for heading, sec_text in split_sections(text):
        out.extend(split_oversized(heading, sec_text))
    return [(h, t) for h, t in out if t.strip()]


# ------------------------------------------------------------------- sources
def load_gap_status(gap_path: pathlib.Path):
    if not gap_path.exists():
        return {}
    gap = json.loads(gap_path.read_text(encoding="utf-8"))
    return {item["old_slug"]: item["status"] for item in gap["items"]}


def old_docs(old_dir: pathlib.Path):
    for f in sorted(old_dir.glob("*/index.md")):
        slug = f.parent.name
        text = f.read_text(encoding="utf-8")
        title_match = re.search(r"^# (.+)", text, re.M)
        title = title_match.group(1).strip() if title_match else slug
        url_match = re.search(r"Source: (https://\S+)", text)
        url = url_match.group(1) if url_match else f"https://refugeemalaysia.org/ (slug: {slug})"
        yield {"slug": slug, "title": title, "url": url, "text": text,
               "path": f"{OLD_SUBDIR}/{slug}"}


def new_docs(root: pathlib.Path, new_dir: pathlib.Path):
    idx_path = new_dir / "_index.json"
    if not idx_path.exists():
        return
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    # Names, not numbers. The index stores topic/category IDs, and an ID is
    # useless in a citation and worse than useless in an embedding -- "22" is
    # not a signal that a chunk is about health. Resolved once here so every
    # chunk carries the words a person would actually search for.
    taxonomy = idx.get("taxonomy") or {}
    topic_names = taxonomy.get("topics") or {}
    cat_names = taxonomy.get("categories") or {}
    audience_names = taxonomy.get("audiences") or {}

    def names(ids, table):
        return [table[str(i)] for i in (ids or []) if str(i) in table]

    for rec in idx["records"]:
        fpath = root / rec["file"]
        if not fpath.exists():
            continue
        yield {
            "slug": rec["path"], "title": rec["title"], "url": rec["url"],
            "text": fpath.read_text(encoding="utf-8"), "path": rec["file"],
            "type": rec["type"], "modified": rec["modified"],
            "topics": names(rec.get("topics"), topic_names),
            "categories": names(rec.get("categories"), cat_names),
            "audiences": names(rec.get("audiences"), audience_names),
        }


def partner_manifest(partner_dir: pathlib.Path):
    """Load the operator-supplied materials manifest, if there is one."""
    idx = partner_dir / "_index.json"
    if not idx.exists():
        return None
    return json.loads(idx.read_text(encoding="utf-8"))


def partner_docs(root: pathlib.Path, manifest, warn=print):
    for rec in manifest["records"]:
        fpath = root / rec["file"]
        if not fpath.exists():
            warn(f"  ! missing partner file, skipped: {rec['file']}")
            continue
        yield {**rec, "text": fpath.read_text(encoding="utf-8")}


def apply_supersessions(chunks, manifest):
    """Mark scraped chunks that newer operator-supplied material overrides.

    Without this the corpus would hold BOTH "children under 18 are not covered"
    (still live on the UNHCR page) and "children 0-17 are covered at RM 150"
    (the posters UNHCR handed out). Retrieval would surface both and the model
    would pick one -- and BOTH would pass every existing safety layer, because
    citation checking asks whether a claim is supported and fact checking asks
    whether a number appears verbatim. Neither asks whether two sources
    disagree. On a question about whether a child's hospital stay is covered,
    that coin flip is the worst outcome this system can produce, so the
    contradiction is resolved here, in data, at build time.
    """
    rules = manifest.get("supersedes") or []
    if not rules:
        return 0
    hits = 0
    for c in chunks:
        if c["source"] == "partner_materials":
            continue
        for rule in rules:
            if rule["match"].lower() in c["text"].lower():
                c["status"] = "superseded"
                c["currency_note"] = (
                    "SUPERSEDED -- do not rely on this. " + rule["reason"]
                )
                hits += 1
                break
    return hits


# --------------------------------------------------------------------- build
def build(root, out_dir, old_dir=None, new_dir=None, partner_dir=None,
          gap_path=None, log=print) -> dict:
    """Write corpus.jsonl + kb_manifest.json into `out_dir`. Returns the manifest.

    `root` is the archive root; the three source directories default to their
    standard places beneath it. `out_dir` is where the corpus lands -- the app
    builds into a staging directory so a person mid-question never reads a
    half-written corpus.

    Missing sources are skipped, not fatal. A fresh install that has just
    crawled the live site has 04_ and nothing else, and that is a legitimate
    (if incomplete) archive -- the manifest records exactly which sources went
    in so nobody has to guess afterwards.
    """
    root = pathlib.Path(root)
    out_dir = pathlib.Path(out_dir)
    old_dir = pathlib.Path(old_dir) if old_dir else root / OLD_SUBDIR
    new_dir = pathlib.Path(new_dir) if new_dir else root / NEW_SUBDIR
    partner_dir = pathlib.Path(partner_dir) if partner_dir else root / PARTNER_SUBDIR
    gap_path = pathlib.Path(gap_path) if gap_path else root / GAP_SUBPATH

    gap_status = load_gap_status(gap_path)
    chunks = []

    if old_dir.is_dir():
        for d in old_docs(old_dir):
            status = gap_status.get(d["slug"], "unmapped")
            for i, (heading, text) in enumerate(chunk_doc(d["text"])):
                chunks.append({
                    "chunk_id": f"old:{d['slug']}:{i}",
                    "source": "old_site_refugeemalaysia_org",
                    "doc_title": d["title"], "doc_path": d["path"], "url": d["url"],
                    "status": status,
                    "currency_note": ("Superseded/dropped on the new site as of 2026-07-21 -- "
                                      "verify before relying on this." if status in
                                      ("dropped", "downgraded", "restructured") else
                                      "Content believed to still exist on the new site "
                                      "(see gap_analysis for exact new URL)."),
                    "section_heading": heading, "chunk_index": i,
                    "word_count": len(text.split()), "text": text,
                })

    for d in new_docs(root, new_dir):
        for i, (heading, text) in enumerate(chunk_doc(d["text"])):
            chunk = {
                "chunk_id": f"new:{d['slug']}:{i}",
                "source": "new_site_help_unhcr_org",
                "doc_title": d["title"], "doc_path": d["path"], "url": d["url"],
                "status": "current", "currency_note": f"Live on help.unhcr.org/malaysia; last modified {d['modified']}.",
                "section_heading": heading, "chunk_index": i,
                "word_count": len(text.split()), "text": text,
                "doc_type": d.get("type"),
            }
            # Only when the site actually said so. Writing "topics": [] onto
            # every chunk would make an unlabelled page indistinguishable from
            # a page UNHCR labelled with nothing, and the archive panel reports
            # coverage from exactly this field.
            for field in ("topics", "categories", "audiences"):
                if d.get(field):
                    chunk[field] = d[field]
            chunks.append(chunk)

    manifest = partner_manifest(partner_dir)
    superseded = 0
    if manifest:
        # The citation a case worker reads has to name the document it came
        # from, not just "a partner". `authority` carries the full title and
        # date of the meeting note so an answer can be traced back to a real,
        # findable record -- and so anyone can tell it apart from the posters,
        # which advertise the promotional child rate without saying it expires.
        provenance = (manifest.get("authority")
                      or f"Provided by {manifest['provided_by']} at the "
                         f"{manifest['occasion']} on {manifest['received']}.")
        provenance += (" Not published on any website -- check the linked page "
                       "for a newer official version.")
        for d in partner_docs(root, manifest, warn=log):
            for i, (heading, text) in enumerate(chunk_doc(d["text"])):
                chunks.append({
                    "chunk_id": f"partner:{pathlib.Path(d['file']).stem}:{i}",
                    "source": "partner_materials",
                    "doc_title": d["title"], "doc_path": d["file"], "url": d["url"],
                    "status": "current",
                    "currency_note": provenance,
                    "section_heading": heading, "chunk_index": i,
                    "word_count": len(text.split()), "text": text,
                    "provided_by": manifest["provided_by"],
                    "received": manifest["received"],
                    # A FIXED citation string, carried on every chunk. It is
                    # written once in _index.json and never rebuilt from parts,
                    # so the same minute is cited identically everywhere and in
                    # every rebuild -- a citation that drifts between answers is
                    # not a citation a case worker can check.
                    "citation": manifest.get("citation"),
                    "source_document": manifest.get("source_document"),
                    # A date after which this material must be re-confirmed.
                    # Time-limited facts -- a promotional price, a temporary
                    # suspension -- are the one kind this archive cannot notice
                    # going stale, because the corpus has no clock. Carry the
                    # deadline as data so the app can warn on its own, rather
                    # than relying on someone remembering in two months.
                    **({"review_by": d["review_by"]} if d.get("review_by") else {}),
                    **({"review_reason": d["review_reason"]}
                       if d.get("review_reason") else {}),
                })
        superseded = apply_supersessions(chunks, manifest)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "corpus.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    by_source = {}
    for c in chunks:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1

    topics = {}
    for c in chunks:
        for name in c.get("topics") or []:
            topics[name] = topics.get(name, 0) + 1

    # `kb`, not `manifest`: the partner-materials manifest is still in scope and
    # is read again below. The build date is stamped, not hardcoded -- it read
    # "2026-07-21" on every rebuild, which is exactly the wrong thing for a file
    # whose job is telling a reader how old the archive is.
    kb = {
        "built": dt.date.today().isoformat(),
        "total_chunks": len(chunks),
        "chunks_by_source": by_source,
        "superseded_chunks": superseded,
        "max_chunk_words": MAX_CHUNK_WORDS,
        # What the archive actually covers, counted rather than claimed. The
        # app shows this so an operator can see a topic is thin BEFORE someone
        # asks about it and gets "not in the archive".
        "chunks_by_topic": dict(sorted(topics.items(), key=lambda kv: -kv[1])),
        "sources": {
            "old_site_refugeemalaysia_org":
                f"{OLD_SUBDIR}/ (captured 2026-07-10, retired site)"
                if old_dir.is_dir() else "not present",
            "new_site_help_unhcr_org":
                f"{NEW_SUBDIR}/ (live site, refreshed by pipeline/refresh.py)"
                if (new_dir / "_index.json").exists() else "not present",
            "partner_materials": (
                f"{PARTNER_SUBDIR}/ (provided by {manifest['provided_by']} at the "
                f"{manifest['occasion']} on {manifest['received']})"
                if manifest else "not present"),
        },
        "note": "Both sites' content is included so nothing dropped by UNHCR is "
                "lost to the CBO -- 'status' and 'currency_note' on every chunk "
                "tell a retrieval system (or a human) whether to trust it as current.",
    }
    (out_dir / "kb_manifest.json").write_text(
        json.dumps(kb, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Wrote {len(chunks)} chunks -> corpus.jsonl  ({by_source})")
    if superseded:
        log(f"Marked {superseded} scraped chunk(s) SUPERSEDED by partner material.")
    return kb
