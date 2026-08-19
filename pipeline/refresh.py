#!/usr/bin/env python3
"""
Refresh the archive from the live site -- safely, and with a human in the loop.

    python pipeline/refresh.py detect     # what changed? (cheap, read-only)
    python pipeline/refresh.py fetch      # pull the changed pages into the archive
    python pipeline/refresh.py build      # rebuild corpus + index into STAGING
    python pipeline/refresh.py diff       # what would change in the answers?
    python pipeline/refresh.py promote    # swap staging in, atomically
    python pipeline/refresh.py status     # where is this refresh up to?

WHY IT IS SPLIT INTO STEPS
--------------------------
`05_intelligence/change_watch/README.md` says a changed hotline number must be
re-read and flagged to case workers "before the old number gets shared again".
A single `refresh` command that scraped and published in one go would delete
that review step -- and the fact most likely to change on a UNHCR help site is
exactly the fact most dangerous to get wrong. So detection is automatic and
publication is not: `promote` is always a human decision.

WHY IT IS KEYED BY WORDPRESS id
-------------------------------
The previous change-watch keyed posts by slug while its baseline keyed them by
date-prefixed slug (scrape_help_unhcr.py names files "<date>-<slug>.md"). The
two never matched, so every run reported all 11 announcements as BOTH new and
removed -- 22 phantom entries burying the real changes. WordPress ids are
stable across retitling and re-slugging, so this keys on those instead.

Politeness (robots.txt, rate limiting, conditional requests, backoff, request
budget) lives in pakpatat/scrape.py.
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pakpatat import config, scrape                            # noqa: E402

SITE = "https://help.unhcr.org/malaysia"
API = f"{SITE}/wp-json/wp/v2"

STATE = config.DATA_DIR / "refresh_state.json"
HTTP_CACHE = config.DATA_DIR / ".http_cache.json"
STAGE_DIR = config.DATA_DIR / "staging"
ARCHIVE_INDEX = "04_help_unhcr_2026/_index.json"

# Facts that hurt when wrong. `diff` reports changes to these separately and
# loudly, because "the page was edited" and "the emergency number was edited"
# should never scroll past at the same volume.
CRITICAL = {
    "phone": re.compile(r"(?:\+?6?0)\d[\d\s\-]{6,12}\d"),
    # NOT case-insensitive, and anchored on a word boundary: "RM" is always
    # capitalised for Malaysian Ringgit, and a case-insensitive match happily
    # found "rm," inside "form," -- noise in the one report that must stay
    # readable enough that nobody learns to skim it.
    "money": re.compile(r"\bRM\s?\d[\d,]*(?:\.\d{2})?"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
}


def _today() -> str:
    return dt.date.today().isoformat()


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"baseline": {}, "captured": None, "pending": None, "history": []}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def seed_from_archive():
    """First run: take the baseline from the capture already on disk.

    Using the operator's own archive rather than the live site means the first
    `detect` reports everything that has drifted SINCE the capture, instead of
    silently declaring today's live site to be the reference point and losing
    that history.
    """
    if not config.ARCHIVE_ROOT:
        return None
    idx = config.ARCHIVE_ROOT / ARCHIVE_INDEX
    if not idx.exists():
        return None
    data = json.loads(idx.read_text(encoding="utf-8"))
    return {
        str(r["id"]): {"path": r["path"], "type": r["type"], "title": r["title"],
                       "url": r["url"], "modified": r["modified"],
                       "text_sha256": r.get("text_sha256")}
        for r in data["records"]
    }, data.get("captured")


# --------------------------------------------------------------------- detect
def live_snapshot(f: scrape.SafeFetcher) -> dict:
    """One cheap pass over the REST collections. ~2 requests for this site."""
    out = {}
    for base, kind in (("pages", "page"), ("posts", "post")):
        page = 1
        while True:
            url = (f"{API}/{base}?per_page=100&page={page}"
                   f"&_fields=id,link,slug,modified,title,type")
            try:
                batch = f.get_json(url, conditional=False)
            except Exception as e:                               # noqa: BLE001
                print(f"  ! {base} page {page}: {e}")
                break
            if not batch:
                break
            for it in batch:
                out[str(it["id"])] = {
                    "path": it["link"].split("/malaysia/", 1)[-1].strip("/") or "_home",
                    "type": kind,
                    "title": re.sub(r"<[^>]+>", "", it["title"]["rendered"]).strip(),
                    "url": it["link"],
                    "modified": it["modified"][:10],
                    "slug": it["slug"],
                }
            if len(batch) < 100:
                break
            page += 1
    return out


def cmd_detect(args) -> int:
    state = load_state()
    if not state["baseline"]:
        seeded = seed_from_archive()
        if seeded:
            state["baseline"], state["captured"] = seeded
            print(f"Seeded baseline from the archive capture "
                  f"({len(state['baseline'])} records, captured {state['captured']}).")
        else:
            print("No baseline and no archive to seed from "
                  "(set PAKPATAT_ARCHIVE). This run will become the baseline.")

    f = scrape.SafeFetcher(HTTP_CACHE, budget=args.budget,
                           min_interval=args.interval)
    print(f"Checking {SITE} ...")
    live = live_snapshot(f)
    f.save_cache()
    print(f"  {len(live)} pages+posts seen  [{f.report()}]")

    old = state["baseline"]
    if not old:
        state["baseline"], state["captured"] = live, _today()
        save_state(state)
        print("Baseline written. Nothing to compare against yet.")
        return 0

    added = [k for k in live if k not in old]
    removed = [k for k in old if k not in live]
    changed = [k for k in live
               if k in old and live[k]["modified"] != old[k].get("modified")]

    print(f"\n=== vs baseline {state['captured']} ===")
    print(f"unchanged : {len(live) - len(added) - len(changed)}")
    print(f"new       : {len(added)}")
    for k in added:
        print(f"   + [{k}] {live[k]['title'][:66]}  ({live[k]['modified']})")
    print(f"removed   : {len(removed)}")
    for k in removed:
        print(f"   - [{k}] {old[k]['title'][:66]}")
    print(f"changed   : {len(changed)}")
    for k in changed:
        print(f"   ~ [{k}] {live[k]['title'][:60]}  "
              f"{old[k].get('modified')} -> {live[k]['modified']}")
        print(f"        {live[k]['url']}")

    pending = sorted(set(added) | set(changed))
    state["pending"] = {"ids": pending, "detected": _today(), "live": live} if pending else None
    save_state(state)

    if pending:
        print(f"\n{len(pending)} page(s) need fetching. "
              f"Next: python pipeline/refresh.py fetch")
    else:
        print("\nArchive is up to date. Nothing to do.")
    return 0


# ---------------------------------------------------------------------- fetch
def _md_tools():
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
        return BeautifulSoup, markdownify
    except ImportError:
        raise SystemExit(
            "Fetching page content needs beautifulsoup4 and markdownify:\n"
            "    pip install -r requirements.txt\n"
            "(`detect` does not need them -- it is standard library only.)")


def to_markdown(raw_html: str, raw_title: str) -> str:
    """Reproduce scrape_help_unhcr.py's conversion EXACTLY.

    If this drifts even in whitespace, every refreshed page shows up as fully
    rewritten in `diff` and the real edit is impossible to see.
    """
    BeautifulSoup, markdownify = _md_tools()
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = BeautifulSoup(raw_title, "html.parser").get_text().strip()
    body = markdownify(str(soup), heading_style="ATX", bullets="-")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return f"# {title}\n\n{body}\n"


def cmd_fetch(args) -> int:
    state = load_state()
    pending = state.get("pending")
    if not pending or not pending["ids"]:
        print("Nothing pending. Run `detect` first.")
        return 0
    if not config.ARCHIVE_ROOT:
        raise SystemExit("PAKPATAT_ARCHIVE is not set -- nowhere to write pages.")

    _md_tools()                              # fail early if deps are missing
    f = scrape.SafeFetcher(HTTP_CACHE, budget=args.budget, min_interval=args.interval)
    root = config.ARCHIVE_ROOT
    live = pending["live"]
    written = []

    print(f"Fetching {len(pending['ids'])} page(s) into {root} ...")
    for wid in pending["ids"]:
        rec = live[wid]
        base = "pages" if rec["type"] == "page" else "posts"
        url = f"{API}/{base}/{wid}?_fields=id,link,slug,date,modified,title,content"
        item = f.get_json(url, conditional=False)
        if not item:
            print(f"  ! [{wid}] no content returned; skipped")
            continue
        text = to_markdown(item["content"]["rendered"], item["title"]["rendered"])

        if rec["type"] == "page":
            d = root / "04_help_unhcr_2026" / "pages" / rec["path"]
            d.mkdir(parents=True, exist_ok=True)
            target = d / "index.md"
            (d / "page.html").write_text(item["content"]["rendered"], encoding="utf-8")
        else:
            d = root / "04_help_unhcr_2026" / "announcements"
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"{item['date'][:10]}-{item['slug'][:70]}.md"

        before = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(text, encoding="utf-8")
        written.append({"id": wid, "title": rec["title"],
                        "file": str(target.relative_to(root)),
                        "url": rec["url"], "was_new": not before})
        print(f"  {'+' if not before else '~'} [{wid}] {rec['title'][:60]}")

    f.save_cache()
    print(f"\n{len(written)} file(s) written  [{f.report()}]")

    # Keep _index.json in step, or build_corpus.py will not see new records.
    _sync_archive_index(root, live, pending["ids"])

    state["pending"]["fetched"] = written
    save_state(state)
    print("Next: python pipeline/refresh.py build")
    return 0


def _sync_archive_index(root: pathlib.Path, live: dict, ids: list) -> None:
    idx_path = root / ARCHIVE_INDEX
    if not idx_path.exists():
        print("  (no _index.json to update)")
        return
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    by_id = {str(r["id"]): r for r in data["records"]}
    for wid in ids:
        rec = live[wid]
        if wid in by_id:
            by_id[wid]["modified"] = rec["modified"]
            by_id[wid]["title"] = rec["title"]
        else:
            fname = (f"04_help_unhcr_2026/pages/{rec['path']}/index.md"
                     if rec["type"] == "page" else
                     f"04_help_unhcr_2026/announcements/{rec['modified']}-{rec['slug'][:70]}.md")
            by_id[wid] = {"id": int(wid), "type": rec["type"], "title": rec["title"],
                          "url": rec["url"], "path": rec["path"], "file": fname,
                          "modified": rec["modified"], "published": rec["modified"],
                          "parent": 0, "topics": [], "categories": []}
    data["records"] = list(by_id.values())
    data["counts"] = {"total": len(data["records"])}
    idx_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  _index.json synced ({len(data['records'])} records)")


# ---------------------------------------------------------------------- build
def cmd_build(args) -> int:
    """Rebuild corpus + index into STAGING. The live index is not touched."""
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    stage_corpus = STAGE_DIR / "corpus.jsonl"
    stage_index = STAGE_DIR / "index"

    import os
    os.environ["PAKPATAT_DATA"] = str(STAGE_DIR)
    print(f"Building corpus into {STAGE_DIR} ...")
    import subprocess
    here = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(here / "pipeline" / "build_corpus.py")],
                       env={**os.environ, "PAKPATAT_DATA": str(STAGE_DIR)})
    if r.returncode != 0:
        raise SystemExit("corpus build failed")

    print("\nEmbedding into staging index ...")
    from pakpatat import index
    index.build(out_dir=stage_index, corpus=stage_corpus)

    state = load_state()
    # `promote` sets pending to None, so setdefault() hands back None rather
    # than a dict. That matters because `build` is legitimately run with nothing
    # pending -- e.g. after adding operator-supplied material by hand, with no
    # site change involved at all.
    if not state.get("pending"):
        state["pending"] = {"ids": [], "detected": _today(), "live": {}}
    state["pending"]["built"] = _today()
    save_state(state)
    print("\nNext: python pipeline/refresh.py diff")
    return 0


# ----------------------------------------------------------------------- diff
def _by_doc(path: pathlib.Path) -> dict:
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            c = json.loads(line)
            out.setdefault(c["doc_path"], []).append(c["text"])
    return {k: "\n".join(v) for k, v in out.items()}


def cmd_diff(args) -> int:
    stage_corpus = STAGE_DIR / "corpus.jsonl"
    if not stage_corpus.exists():
        print("No staged corpus. Run `build` first.")
        return 1
    old, new = _by_doc(config.CORPUS), _by_doc(stage_corpus)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in new if k in old and new[k] != old[k])

    print(f"=== corpus: live vs staged ===")
    print(f"documents  live={len(old)}  staged={len(new)}")
    print(f"added={len(added)} removed={len(removed)} changed={len(changed)}\n")
    alarms = 0
    for k in added:
        print(f"  + {k}")
        # A NEW document introducing a hotline or a fee is every bit as
        # consequential as an edited one -- an earlier version of this report
        # only inspected `changed`, so a freshly added page could bring in a
        # phone number and the summary would still say "nothing changed".
        for label, pat in CRITICAL.items():
            found = sorted(set(pat.findall(new[k])))
            if found:
                alarms += 1
                print(f"      !! NEW {label.upper()}: {found}")
    for k in removed:
        print(f"  - {k}")
        for label, pat in CRITICAL.items():
            found = sorted(set(pat.findall(old[k])))
            if found:
                alarms += 1
                print(f"      !! {label.upper()} LOST WITH THIS PAGE: {found}")

    for k in changed:
        print(f"\n  ~ {k}")
        for label, pat in CRITICAL.items():
            was, now = set(pat.findall(old[k])), set(pat.findall(new[k]))
            gone, fresh = was - now, now - was
            if gone or fresh:
                alarms += 1
                print(f"      !! {label.upper()} CHANGED")
                if gone:
                    print(f"         removed: {sorted(gone)}")
                if fresh:
                    print(f"         added  : {sorted(fresh)}")
        if args.text:
            import difflib
            d = list(difflib.unified_diff(old[k].splitlines(), new[k].splitlines(),
                                          lineterm="", n=1))
            for line in d[2:42]:
                print(f"      {line}")

    print("\n" + "=" * 62)
    if alarms:
        print(f"{alarms} CRITICAL-FACT CHANGE(S). Read every one above and confirm")
        print("against the live page before promoting. These are the values that")
        print("send someone to a wrong number.")
    else:
        print("No phone number, fee or email changed.")
    print("Promote with: python pipeline/refresh.py promote")
    return 0


# -------------------------------------------------------------------- promote
def cmd_promote(args) -> int:
    """Swap staging into place atomically, keeping one rollback copy."""
    stage_corpus, stage_index = STAGE_DIR / "corpus.jsonl", STAGE_DIR / "index"
    if not (stage_corpus.exists() and (stage_index / "meta.json").exists()):
        print("Nothing staged to promote. Run `build` first.")
        return 1
    if not args.yes:
        print("This replaces the archive the app answers from.")
        if input("Type 'promote' to confirm: ").strip() != "promote":
            print("Aborted.")
            return 1

    backup = config.DATA_DIR / "previous"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    if config.CORPUS.exists():
        shutil.copy2(config.CORPUS, backup / "corpus.jsonl")
    if config.INDEX_DIR.exists():
        shutil.copytree(config.INDEX_DIR, backup / "index")

    # Directory rename is atomic on POSIX: a reader either sees the whole old
    # index or the whole new one, never a half-written mix.
    tmp = config.DATA_DIR / "index.incoming"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(stage_index, tmp)
    old = config.DATA_DIR / "index.retiring"
    if config.INDEX_DIR.exists():
        config.INDEX_DIR.rename(old)
    tmp.rename(config.INDEX_DIR)
    if old.exists():
        shutil.rmtree(old)
    shutil.copy2(stage_corpus, config.CORPUS)

    state = load_state()
    live = (state.get("pending") or {}).get("live")
    if live:
        state["baseline"], state["captured"] = live, _today()
    state.setdefault("history", []).append(
        {"promoted": _today(), "ids": (state.get("pending") or {}).get("ids", [])})
    state["pending"] = None
    save_state(state)

    print(f"Promoted. Rollback copy in {backup}")
    # retrieve.py caches the index in a module-level _STATE on first use, so a
    # running app keeps serving the OLD index no matter how many questions are
    # asked. Quitting and reopening is the only thing that picks this up.
    print("QUIT AND REOPEN the app -- a running copy keeps the old index in")
    print("memory and will go on answering from it.")
    return 0


def cmd_bootstrap(args) -> int:
    """Build the live-site archive from scratch, for an install that has none.

    `detect`/`fetch` keep an existing archive current; they cannot create one,
    so a fresh clone previously dead-ended: build_index.py asks for a corpus,
    build_corpus.py asks for an archive, and nothing could produce one. This
    closes that loop for the 334 chunks that come from the live site.

    It does NOT recover the other two sources, and cannot:
      - the retired refugeemalaysia.org capture (262 chunks) -- the site was
        taken down on 2026-07-14, so that capture is the only copy in existence
      - operator-supplied partner materials -- never published anywhere
    Those must be copied from an existing operator. That is worth knowing
    before anyone trusts this as a disaster-recovery plan.
    """
    if not config.ARCHIVE_ROOT:
        raise SystemExit("PAKPATAT_ARCHIVE is not set -- nowhere to write the archive.")
    _md_tools()
    root = config.ARCHIVE_ROOT
    out = root / "04_help_unhcr_2026"
    if (out / "_index.json").exists() and not args.force:
        raise SystemExit(f"{out}/_index.json already exists. Use `detect`+`fetch` to "
                         f"update it, or pass --force to re-capture from scratch.")

    f = scrape.SafeFetcher(HTTP_CACHE, budget=args.budget, min_interval=args.interval)
    print(f"Capturing {SITE} into {out}")
    print("This is a full site capture -- slow on purpose "
          f"({args.interval}s between requests).")

    records = []
    for base, kind in (("pages", "page"), ("posts", "post")):
        page = 1
        while True:
            url = (f"{API}/{base}?per_page=100&page={page}"
                   f"&_fields=id,link,slug,date,modified,title,content")
            batch = f.get_json(url, conditional=False)
            if not batch:
                break
            for it in batch:
                text = to_markdown(it["content"]["rendered"], it["title"]["rendered"])
                path = it["link"].split("/malaysia/", 1)[-1].strip("/") or "_home"
                if kind == "page":
                    d = out / "pages" / path
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "index.md").write_text(text, encoding="utf-8")
                    (d / "page.html").write_text(it["content"]["rendered"], encoding="utf-8")
                    rel = f"04_help_unhcr_2026/pages/{path}/index.md"
                else:
                    d = out / "announcements"
                    d.mkdir(parents=True, exist_ok=True)
                    name = f"{it['date'][:10]}-{it['slug'][:70]}.md"
                    (d / name).write_text(text, encoding="utf-8")
                    rel = f"04_help_unhcr_2026/announcements/{name}"
                records.append({
                    "id": it["id"], "type": kind,
                    "title": re.sub(r"<[^>]+>", "",
                                    it["title"]["rendered"]).strip(),
                    "url": it["link"], "path": path, "file": rel,
                    "published": it["date"][:10], "modified": it["modified"][:10],
                    "parent": 0, "topics": [], "categories": [],
                })
                print(f"  + {records[-1]['title'][:64]}")
            if len(batch) < 100:
                break
            page += 1

    out.mkdir(parents=True, exist_ok=True)
    (out / "_index.json").write_text(json.dumps(
        {"source": SITE, "captured": _today(),
         "counts": {"total": len(records)}, "taxonomy": {},
         "records": records, "media": []},
        indent=2, ensure_ascii=False), encoding="utf-8")
    f.save_cache()

    print(f"\nCaptured {len(records)} pages+posts  [{f.report()}]")
    print("NOTE: this recovers the live site only. The retired-site capture and")
    print("any partner materials must be copied from an existing install.")
    print("\nNext: python pipeline/build_corpus.py && python build_index.py")
    return 0


def cmd_status(args) -> int:
    s = load_state()
    print(f"baseline captured : {s.get('captured') or '(none)'}")
    print(f"baseline records  : {len(s.get('baseline') or {})}")
    p = s.get("pending")
    if p:
        print(f"pending           : {len(p.get('ids', []))} page(s) detected {p.get('detected')}")
        print(f"  fetched         : {len(p.get('fetched') or [])}")
        print(f"  built           : {p.get('built') or 'no'}")
    else:
        print("pending           : nothing")
    for h in (s.get("history") or [])[-5:]:
        print(f"  promoted {h['promoted']}: {len(h['ids'])} page(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--interval", type=float, default=scrape.MIN_INTERVAL,
                       help="minimum seconds between requests (default %(default)s)")
        p.add_argument("--budget", type=int, default=scrape.DEFAULT_BUDGET,
                       help="hard cap on requests this run (default %(default)s)")
        return p

    b = common(sub.add_parser("bootstrap", help="full first capture, for an install with no archive"))
    b.add_argument("--force", action="store_true", help="re-capture even if an archive exists")
    b.set_defaults(fn=cmd_bootstrap)
    common(sub.add_parser("detect", help="what changed on the live site")).set_defaults(fn=cmd_detect)
    common(sub.add_parser("fetch", help="pull changed pages into the archive")).set_defaults(fn=cmd_fetch)
    sub.add_parser("build", help="rebuild corpus+index into staging").set_defaults(fn=cmd_build)
    d = sub.add_parser("diff", help="what would change in the answers")
    d.add_argument("--text", action="store_true", help="show line-level text diff too")
    d.set_defaults(fn=cmd_diff)
    p = sub.add_parser("promote", help="swap staging in (atomic)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(fn=cmd_promote)
    sub.add_parser("status", help="where this refresh is up to").set_defaults(fn=cmd_status)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
