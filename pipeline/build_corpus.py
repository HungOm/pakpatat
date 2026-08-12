#!/usr/bin/env python3
"""
Build a single, chunked, metadata-tagged corpus across BOTH archives --
the old refugeemalaysia.org capture (01_support_topics/) and the new
help.unhcr.org/malaysia capture (04_help_unhcr_2026/) -- ready to embed for a
future MCP/RAG server.

Every chunk carries enough metadata to answer, at query time:
  - which site it came from
  - whether it's still current (new site) or superseded/dropped (old site,
    per ../gap_analysis/gap_analysis.json)
  - the source URL, for citing back to UNHCR

Chunking: split on markdown ## headings (natural topic boundaries in these
pages); anything still over ~350 words gets further split on paragraph
breaks. This keeps chunks small enough for an embedding model's context
window while keeping each chunk semantically whole.

This script reads an archive that the OPERATOR already holds locally. No source
material ships with this repository (see NOTICE.md) -- point PAKPATAT_ARCHIVE
at your own copy:

    export PAKPATAT_ARCHIVE=~/path/to/archive
    python pipeline/build_corpus.py

Expected layout under PAKPATAT_ARCHIVE (override any of them by env var):
  01_support_topics/            retired-site capture, one dir per page
  04_help_unhcr_2026/           live-site capture + _index.json
  05_intelligence/gap_analysis/gap_analysis.json   (optional)

Output (into PAKPATAT_DATA, default ./data):
  corpus.jsonl     one JSON object per chunk (the embeddable corpus)
  kb_manifest.json summary: doc count, chunk count, per-source breakdown
"""
import datetime as dt
import json, os, pathlib, re, sys

_env = os.getenv("PAKPATAT_ARCHIVE")
if not _env:
    sys.exit(
        "PAKPATAT_ARCHIVE is not set.\n"
        "Point it at your local copy of the source archive, e.g.\n"
        "    export PAKPATAT_ARCHIVE=~/Desktop/refugee_malaysia\n"
        "This repository ships no archive content of its own -- see NOTICE.md."
    )

ROOT = pathlib.Path(_env).expanduser().resolve()
OLD_DIR = pathlib.Path(os.getenv("PAKPATAT_OLD_DIR", ROOT / "01_support_topics"))
NEW_DIR = pathlib.Path(os.getenv("PAKPATAT_NEW_DIR", ROOT / "04_help_unhcr_2026"))
GAP = pathlib.Path(os.getenv("PAKPATAT_GAP",
                             ROOT / "05_intelligence" / "gap_analysis" / "gap_analysis.json"))
# Material the operator was given directly (posters, circulars, briefing packs)
# rather than scraped. Optional -- absent on a fresh install.
PARTNER_DIR = pathlib.Path(os.getenv("PAKPATAT_PARTNER", ROOT / "07_partner_materials"))
OUT = pathlib.Path(os.getenv("PAKPATAT_DATA",
                             pathlib.Path(__file__).resolve().parents[1] / "data"))

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


def load_gap_status():
    if not GAP.exists():
        return {}
    gap = json.loads(GAP.read_text())
    return {item["old_slug"]: item["status"] for item in gap["items"]}


def old_docs():
    for f in sorted(OLD_DIR.glob("*/index.md")):
        slug = f.parent.name
        text = f.read_text(encoding="utf-8")
        title_match = re.search(r"^# (.+)", text, re.M)
        title = title_match.group(1).strip() if title_match else slug
        url_match = re.search(r"Source: (https://\S+)", text)
        url = url_match.group(1) if url_match else f"https://refugeemalaysia.org/ (slug: {slug})"
        yield {"slug": slug, "title": title, "url": url, "text": text, "path": f"01_support_topics/{slug}"}


def new_docs():
    idx = json.loads((NEW_DIR / "_index.json").read_text())
    for rec in idx["records"]:
        fpath = ROOT / rec["file"]
        if not fpath.exists():
            continue
        yield {
            "slug": rec["path"], "title": rec["title"], "url": rec["url"],
            "text": fpath.read_text(encoding="utf-8"), "path": rec["file"],
            "type": rec["type"], "modified": rec["modified"],
        }


def partner_manifest():
    """Load the operator-supplied materials manifest, if there is one."""
    idx = PARTNER_DIR / "_index.json"
    if not idx.exists():
        return None
    return json.loads(idx.read_text(encoding="utf-8"))


def partner_docs(manifest):
    for rec in manifest["records"]:
        fpath = ROOT / rec["file"]
        if not fpath.exists():
            print(f"  ! missing partner file, skipped: {rec['file']}")
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


def build():
    gap_status = load_gap_status()
    chunks = []

    for d in old_docs():
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

    for d in new_docs():
        for i, (heading, text) in enumerate(chunk_doc(d["text"])):
            chunks.append({
                "chunk_id": f"new:{d['slug']}:{i}",
                "source": "new_site_help_unhcr_org",
                "doc_title": d["title"], "doc_path": d["path"], "url": d["url"],
                "status": "current", "currency_note": f"Live on help.unhcr.org/malaysia; last modified {d['modified']}.",
                "section_heading": heading, "chunk_index": i,
                "word_count": len(text.split()), "text": text,
                "doc_type": d.get("type"),
            })

    manifest = partner_manifest()
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
        for d in partner_docs(manifest):
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

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "corpus.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    by_source = {}
    for c in chunks:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1

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
        "sources": {
            "old_site_refugeemalaysia_org": "01_support_topics/ (captured 2026-07-10, retired site)",
            "new_site_help_unhcr_org": "04_help_unhcr_2026/ (live site, refreshed by pipeline/refresh.py)",
            "partner_materials": (
                f"07_partner_materials/ (provided by {manifest['provided_by']} at the "
                f"{manifest['occasion']} on {manifest['received']})"
                if manifest else "not present"),
        },
        "note": "Both sites' content is included so nothing dropped by UNHCR is "
                "lost to the CBO -- 'status' and 'currency_note' on every chunk "
                "tell a retrieval system (or a human) whether to trust it as current.",
    }
    (OUT / "kb_manifest.json").write_text(json.dumps(kb, indent=2, ensure_ascii=False))
    print(f"Wrote {len(chunks)} chunks -> corpus.jsonl  ({by_source})")
    if superseded:
        print(f"Marked {superseded} scraped chunk(s) SUPERSEDED by partner material.")


if __name__ == "__main__":
    build()
