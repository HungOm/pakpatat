# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
Getting the archive, and keeping it current, from inside the running app.

pipeline/refresh.py already does all of this from a terminal, and does it well.
It is also unreachable to almost everyone who will ever use this program: an
installed .app has no `pipeline/` directory, no `python` on PATH and no
PAKPATAT_ARCHIVE, so the operator's only route to fresh guidance was to find a
developer. Meanwhile the one number most likely to change on a UNHCR help site
is the one most dangerous to be stale about. A refresh path that only exists
for developers is a refresh path that does not exist.

So this module is the same work, in-process, streaming progress, driven from
two buttons. It shares refresh.py's state file and its politeness layer, so the
command line and the app cannot disagree about what has been captured.

    capture()   first copy: crawl the whole site into a fresh archive
    check()     what changed since the last capture? (2-4 requests, no bodies)
    stage()     pull the changed pages, rebuild corpus + index into staging
    apply()     swap staging in, atomically, keeping one rollback copy

WHY capture AND stage BOTH EXIST, AND WHY apply IS SEPARATE
-----------------------------------------------------------
An install with nothing has nothing to lose: capture takes what the site says
and publishes it, because the alternative on that screen is an app that cannot
answer at all.

An install that already works is a different question. Replacing a living
archive means the answer a case worker reads in ten minutes may differ from the
one they read this morning, and if a hotline number moved, "differ" means the
old one is gone. So an update is fetched, built and DIFFED first, and a person
presses Apply -- exactly the review step 05_intelligence/change_watch/README.md
asks for, moved from a terminal into the window where the operator already is.

POLITENESS
----------
Every request goes through pakpatat/scrape.py: robots.txt is fetched and
obeyed, there is a real rate limit with jitter, conditional requests mean an
unchanged page costs a 304 with no body, backoff honours Retry-After, and a
hard per-run request budget means a bug in a loop cannot become a flood. The
budget here is computed from what the site actually reports rather than left at
a guess, so a site that grows does not silently get truncated -- and a site
that explodes still cannot be hammered.
"""
import datetime as dt
import hashlib
import json
import pathlib
import re
import shutil
import urllib.parse

from . import config, scrape

SITE = "https://help.unhcr.org/malaysia"
API = f"{SITE}/wp-json/wp/v2"

# The same three files pipeline/refresh.py uses. Shared deliberately: two
# baselines for one archive would each report the other's work as a change.
STATE = config.DATA_DIR / "refresh_state.json"
HTTP_CACHE = config.DATA_DIR / ".http_cache.json"
STAGE_DIR = config.DATA_DIR / "staging"

NEW_SUBDIR = "04_help_unhcr_2026"
ARCHIVE_INDEX = f"{NEW_SUBDIR}/_index.json"

# WordPress exposes its own furniture through the same REST API as its content:
# menus, block templates, global styles, font faces. None of it is guidance and
# all of it would end up embedded as searchable text. Discovery is dynamic
# precisely so a content type UNHCR adds later is picked up without a code
# change -- so the exclusion list names the machinery rather than allow-listing
# the content, which would put us back where we started.
SKIP_TYPES = {
    "attachment",            # handled separately: catalogued, never chunked
    "nav_menu_item", "wp_block", "wp_template", "wp_template_part",
    "wp_global_styles", "wp_navigation", "wp_font_family", "wp_font_face",
    "wp_navigation_link",
}

# Facts that hurt when wrong. `stage` reports changes to these separately and
# loudly, because "the page was edited" and "the emergency number was edited"
# must never scroll past at the same volume. Kept identical to
# pipeline/refresh.py's copy on purpose -- two definitions of "critical" would
# mean the terminal and the window disagreeing about what needs a human.
CRITICAL = {
    "phone": re.compile(r"(?:\+?6?0)\d[\d\s\-]{6,12}\d"),
    "money": re.compile(r"\bRM\s?\d[\d,]*(?:\.\d{2})?"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
}

VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "facebook.com/watch")
DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
FILE_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")


class Unavailable(RuntimeError):
    """This step cannot run right now, with the reason why."""


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _today() -> str:
    return dt.date.today().isoformat()


# ------------------------------------------------------------------ location
def root() -> pathlib.Path:
    """Where the archive lives.

    PAKPATAT_ARCHIVE when the operator has set it -- a developer with a curated
    archive keeps working exactly as before. Otherwise a directory inside the
    app's own writable data folder, because an installed build has no such
    variable and "set an environment variable first" is not an instruction that
    reaches a case worker. Without this fallback the download button would have
    nowhere to put what it downloads.
    """
    return config.ARCHIVE_ROOT or (config.DATA_DIR / "archive")


def _deps():
    """The two packages that turn WordPress HTML into the archive's markdown.

    Both are core requirements now (requirements.txt) precisely so this check
    should never fail on a properly built install -- but it is checked up
    front and by name anyway, because the alternative to checking is the crawl
    running, writing nothing usable, and reporting success. That is the worst
    of the three possible outcomes, and the one the button must never produce.
    """
    try:
        from bs4 import BeautifulSoup                          # noqa: F401
        from markdownify import markdownify                    # noqa: F401
    except ImportError as e:
        raise Unavailable(
            "This copy of the app is missing a required component "
            "(beautifulsoup4/markdownify) and cannot read web pages. " +
            ("Reinstall the app, or ask whoever gave it to you for an "
             "updated copy." if config.FROZEN else
             "Run `pip install -r requirements.txt`.")
        ) from e


# --------------------------------------------------------------------- state
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass          # a corrupt state file costs one full check, not a crash
    return {"baseline": {}, "captured": None, "pending": None, "history": []}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------- fetching
def _fetcher(budget: int, interval: float | None = None) -> scrape.SafeFetcher:
    return scrape.SafeFetcher(
        HTTP_CACHE, budget=budget,
        min_interval=interval if interval else scrape.MIN_INTERVAL,
        verbose=False,
    )


def content_types(f: scrape.SafeFetcher) -> list[tuple[str, str]]:
    """Every public content type this site publishes, as (rest_base, key).

    Asked rather than assumed. The previous capture hardcoded pages and posts,
    which is what the site had in July; it also has a `contact_address` type
    that a hardcoded list would go on missing forever, and nothing stops UNHCR
    from adding a 'legal-aid' or 'education' type next month. One request buys
    the whole answer.
    """
    try:
        types = f.get_json(f"{API}/types", conditional=False) or {}
    except Exception:                                          # noqa: BLE001
        types = {}
    if not types:
        return [("pages", "page"), ("posts", "post")]          # the known minimum
    out = []
    for key, spec in types.items():
        base = spec.get("rest_base")
        if not base or key in SKIP_TYPES or "(?P<" in base:
            continue
        out.append((base, key))
    # Pages first so the crawl reports the guidance the site is built around
    # before it reaches the announcements stream.
    out.sort(key=lambda kb: (kb[1] != "page", kb[1] != "post", kb[1]))
    return out


def taxonomies(f: scrape.SafeFetcher) -> dict:
    """Resolve UNHCR's own topic labels into {taxonomy: {id: name}}.

    These are the words UNHCR files its own guidance under -- Health,
    Detention and deportation, Gender-based violence, Education. The previous
    capture wrote `"topics": []` on every record and dropped them, so the
    corpus knew what a page SAID and never what it was ABOUT. They are carried
    into every chunk now (pakpatat/corpus.py) and reported as coverage, which
    is the difference between "the archive has 617 passages" and "the archive
    has nothing at all on education".
    """
    out = {}
    for base in ("topics", "audiences", "categories", "tags"):
        try:
            terms = f.get_json(f"{API}/{base}?per_page=100&_fields=id,name",
                               conditional=False)
        except Exception:                                      # noqa: BLE001
            continue
        if terms:
            out[base] = {str(t["id"]): t["name"] for t in terms}
    return out


def _paginate(f: scrape.SafeFetcher, base: str, fields: str, per_page: int = 100):
    page = 1
    while True:
        url = f"{API}/{base}?per_page={per_page}&page={page}&_fields={fields}"
        try:
            batch = f.get_json(url, conditional=False)
        except scrape.Refused:
            # Budget exhausted or robots.txt says no: stop, loudly. Swallowing
            # this here would turn "the crawl hit its safety limit" into "the
            # site apparently only has 30 pages" -- a wrong, quiet answer is
            # worse than the request that failed to produce one at all.
            raise
        except Exception:                                      # noqa: BLE001
            # One content type failing for some OTHER reason (a malformed
            # response, a transient error already past its retries) must not
            # lose the other five -- that partial result is still worth having.
            return
        if not batch:
            return
        yield from batch
        if len(batch) < per_page:
            return
        page += 1


def _path_for(link: str) -> str:
    return link.split("/malaysia/", 1)[-1].strip("/") or "_home"


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def snapshot(f: scrape.SafeFetcher, types=None) -> dict:
    """One cheap pass over every collection -- titles and dates, no bodies."""
    types = types if types is not None else content_types(f)
    out = {}
    for base, kind in types:
        for it in _paginate(f, base,
                            "id,link,slug,modified,date,title,type,parent"):
            out[str(it["id"])] = {
                "path": _path_for(it.get("link") or ""),
                "type": kind,
                "title": _strip_tags((it.get("title") or {}).get("rendered", "")),
                "url": it.get("link"),
                "modified": (it.get("modified") or "")[:10],
                "published": (it.get("date") or "")[:10],
                "slug": it.get("slug"),
                "parent": it.get("parent") or 0,
                "rest_base": base,
            }
    return out


# ------------------------------------------------------------- html -> markdown
def to_markdown(raw_html: str, raw_title: str) -> str:
    """Reproduce the original scraper's conversion EXACTLY.

    If this drifts even in whitespace, every refreshed page shows up as fully
    rewritten in the review screen and the real edit is impossible to see --
    which turns the human check into a formality, which is how a changed
    hotline number gets waved through.
    """
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = BeautifulSoup(raw_title, "html.parser").get_text().strip()
    body = markdownify(str(soup), heading_style="ATX", bullets="-")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return f"# {title}\n\n{body}\n"


def classify_links(raw_html: str) -> dict:
    """Sort a page's outbound links into the buckets the archive index keeps.

    Not used by retrieval -- kept because the archive index has always carried
    it, and because it is how an operator finds the PDFs and videos UNHCR
    publishes alongside the prose, which this tool cannot read but a person can.
    """
    out = {"drive": [], "video": [], "file": [], "internal": [], "external": []}
    for href in re.findall(r'href=["\']([^"\'#][^"\']*)["\']', raw_html or ""):
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        low = href.lower()
        host = urllib.parse.urlparse(href).netloc.lower()
        if any(h in low for h in DRIVE_HOSTS):
            bucket = "drive"
        elif any(h in low for h in VIDEO_HOSTS):
            bucket = "video"
        elif low.split("?")[0].endswith(FILE_SUFFIXES) or "/wp-content/uploads/" in low:
            bucket = "file"
        elif not host or "help.unhcr.org" in host:
            bucket = "internal"
        else:
            bucket = "external"
        if href not in out[bucket]:
            out[bucket].append(href)
    return {k: sorted(v) for k, v in out.items()}


def _write_doc(arc: pathlib.Path, item: dict, rec: dict) -> tuple[str, str]:
    """Write one page to disk in the archive's layout. Returns (relpath, text).

    Pages become a directory with index.md beside the raw page.html; posts
    become one date-prefixed file in announcements/. That split is the existing
    archive's own shape and build_corpus.py reads it as given -- this writes
    into the blueprint rather than inventing a second one.
    """
    text = to_markdown(item["content"]["rendered"], item["title"]["rendered"])
    if rec["type"] == "post":
        d = arc / "announcements"
        d.mkdir(parents=True, exist_ok=True)
        name = f"{(item.get('date') or '')[:10]}-{(item.get('slug') or str(item['id']))[:70]}.md"
        (d / name).write_text(text, encoding="utf-8")
        return f"{NEW_SUBDIR}/announcements/{name}", text

    sub = "pages" if rec["type"] == "page" else rec["type"]
    d = arc / sub / rec["path"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.md").write_text(text, encoding="utf-8")
    (d / "page.html").write_text(item["content"]["rendered"], encoding="utf-8")
    return f"{NEW_SUBDIR}/{sub}/{rec['path']}/index.md", text


def _record(item: dict, rec: dict, relfile: str, text: str) -> dict:
    """One row of _index.json, in the shape the existing archive already uses."""
    return {
        "id": item["id"],
        "type": rec["type"],
        "title": rec["title"],
        "url": rec["url"],
        "path": rec["path"],
        "file": relfile,
        "published": rec.get("published") or (item.get("date") or "")[:10],
        "modified": rec["modified"],
        "parent": rec.get("parent") or 0,
        "topics": item.get("topics") or [],
        "categories": item.get("categories") or [],
        "audiences": item.get("audiences") or [],
        "word_count": len(text.split()),
        # Content hash, so a page whose `modified` date moved but whose words
        # did not can be recognised as unchanged. WordPress bumps `modified`
        # for edits nobody reading the page would notice.
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "links": classify_links(item["content"]["rendered"]),
    }


def _media(f: scrape.SafeFetcher) -> list[dict]:
    """Catalogue what UNHCR publishes that is not text.

    Listed, never downloaded and never chunked: posters, infographics and PDFs
    carry real guidance, but this app answers from text it can quote and verify
    a citation against. Recording them means an operator can SEE that a page
    points at a PDF the assistant cannot read, instead of the archive quietly
    behaving as though the page had nothing on it.
    """
    out = []
    for it in _paginate(f, "media",
                        "id,source_url,mime_type,title,media_details"):
        url = it.get("source_url") or ""
        details = it.get("media_details") or {}
        out.append({
            "id": it["id"],
            "file": f"media/{url.rsplit('/', 1)[-1]}" if url else "",
            "mime": it.get("mime_type"),
            "title": _strip_tags((it.get("title") or {}).get("rendered", "")),
            "url": url,
            "bytes": details.get("filesize"),
        })
    return out


# ------------------------------------------------------------------- capture
def capture(progress, budget: int = 400, interval: float | None = None) -> dict:
    """Crawl the whole site into a fresh archive. The first-copy path.

    Slow on purpose. At the default two seconds a request this takes a couple
    of minutes for the ~52 documents the site currently holds, and every one of
    those seconds is the difference between a light client and a nuisance.
    """
    _deps()

    # STEP 0, when an operator has configured one: the half of the archive no
    # crawl can reach. refugeemalaysia.org is gone and partner materials were
    # never published, so those pages exist only in copies people hold. Fetched
    # first, so the live crawl below writes over the top of it rather than the
    # other way round -- current guidance should win any overlap, which is the
    # same rule the ranking applies (config.SUPERSEDED_BY_LIVE_WEIGHT).
    #
    # Entirely optional. Nothing is configured by default and an install
    # without it behaves exactly as before: live site only.
    from . import bundle
    bundled = None
    if bundle.configured():
        try:
            bundled = bundle.fetch(progress)
        except bundle.Unavailable as e:
            # Not fatal. A wrong link or an expired token should not block the
            # live crawl, which still produces a working app -- it should say
            # so and carry on, because the alternative is an install with
            # nothing at all because one optional URL was mistyped.
            progress({"stage": "bundle_skipped", "detail": str(e)[:200]})

    arc = root() / NEW_SUBDIR
    arc.mkdir(parents=True, exist_ok=True)

    f = _fetcher(budget, interval)
    progress({"stage": "connecting"})

    types = content_types(f)
    tax = taxonomies(f)
    listing = snapshot(f, types)
    total = len(listing)
    if not total:
        raise Unavailable(
            "Could not read anything from help.unhcr.org. Check this computer "
            "is online, then try again."
        )

    # Budget is now a known quantity rather than a guess: one request per
    # document, plus what discovery already spent, plus headroom for media
    # paging and retries. Raised here rather than started high, so a runaway
    # loop before this point still hits the low ceiling.
    f.budget = max(f.budget, f.used + total + 40)

    records, done = [], 0
    for wid, rec in listing.items():
        progress({"stage": "crawling", "count": done, "total": total,
                  "detail": rec["title"][:60]})
        url = (f"{API}/{rec['rest_base']}/{wid}"
               f"?_fields=id,link,slug,date,modified,title,content,"
               f"topics,categories,audiences")
        try:
            item = f.get_json(url, conditional=False)
        except scrape.Refused:
            raise          # budget/robots stop the whole run, loudly -- see _paginate
        except Exception:                                      # noqa: BLE001
            item = None
        done += 1
        if not item or not (item.get("content") or {}).get("rendered"):
            continue
        relfile, text = _write_doc(arc, item, rec)
        records.append(_record(item, rec, relfile, text))

    progress({"stage": "cataloguing", "count": done, "total": total})
    media = _media(f)

    (arc / "_index.json").write_text(json.dumps({
        "source": SITE,
        "captured": _now(),
        "counts": {"total": len(records)},
        "taxonomy": tax,
        "records": records,
        "media": media,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    f.save_cache()

    state = load_state()
    state["baseline"] = listing
    state["captured"] = _today()
    state["last_checked"] = _now()
    state["pending"] = None
    save_state(state)

    # A crawl that stops at raw pages is not an archive this app can answer
    # from -- nothing chunks or embeds itself. Built straight into the live
    # data directory rather than staged: an install with no corpus has no
    # index for a reader to be mid-question against, so there is nothing here
    # for a staging copy to protect. The index write is still done through the
    # same atomic rename rebuild_index uses, on the off chance this window is
    # closed mid-build -- a killed process should leave either no index or a
    # whole one, never half of one.
    progress({"stage": "chunking"})
    from . import corpus, index, retrieve
    kb = corpus.build(root=root(), out_dir=config.DATA_DIR, log=lambda *a: None)

    def relay(stage_name, count, total_):
        progress({"stage": stage_name, "count": count, "total": total_})

    building = config.DATA_DIR / "index.building"
    if building.exists():
        shutil.rmtree(building)
    index.build(out_dir=building, corpus=config.CORPUS, progress=relay)
    progress({"stage": "installing"})
    from .firstrun import _swap_in
    _swap_in(building)
    retrieve.reset()

    return {"documents": len(records), "media": len(media),
            "topics": len(tax.get("topics") or {}), "chunks": kb["total_chunks"],
            "http": f.report(),
            "bundle": bundled,
            "sources": kb.get("chunks_by_source") or {}}


# --------------------------------------------------------------------- check
def check(progress=None, budget: int = 60, interval: float | None = None) -> dict:
    """What has changed since the last capture? Cheap, read-only, no bodies.

    Two to four requests for the whole site. This is what a "check for updates"
    button can honestly do in a few seconds without downloading anything.
    """
    if progress:
        progress({"stage": "checking"})
    f = _fetcher(budget, interval)
    types = content_types(f)
    live = snapshot(f, types)
    f.save_cache()

    if not live:
        raise Unavailable(
            "Could not reach help.unhcr.org. Check this computer is online."
        )

    state = load_state()
    old = state.get("baseline") or {}

    if not old:
        # Nothing to compare against: record what is there and say so, rather
        # than reporting all 52 documents as brand-new changes.
        state["baseline"], state["captured"] = live, _today()
        state["last_checked"] = _now()
        save_state(state)
        return {"first_run": True, "added": [], "changed": [], "removed": [],
                "total": len(live), "checked": state["last_checked"]}

    def row(wid, src, was=None):
        r = dict(src[wid])
        r["id"] = wid
        if was:
            r["was_modified"] = was
        return r

    added = [row(k, live) for k in live if k not in old]
    changed = [row(k, live, old[k].get("modified")) for k in live
               if k in old and live[k]["modified"] != old[k].get("modified")]
    removed = [row(k, old) for k in old if k not in live]

    state["last_checked"] = _now()
    ids = sorted({r["id"] for r in added} | {r["id"] for r in changed})
    state["pending"] = ({"ids": ids, "detected": _today(), "live": live}
                        if ids else None)
    save_state(state)

    return {"first_run": False, "added": added, "changed": changed,
            "removed": removed, "total": len(live),
            "checked": state["last_checked"]}


# --------------------------------------------------------------------- stage
def _sync_index(arc: pathlib.Path, updated: dict, tax: dict) -> None:
    """Fold refreshed records into _index.json, keeping everything else.

    build_corpus.py reads this file, not the directory, so a page written to
    disk without a row here is a page the assistant will never see.
    """
    path = arc / "_index.json"
    data = ({"source": SITE, "captured": _now(), "counts": {}, "taxonomy": {},
             "records": [], "media": []}
            if not path.exists()
            else json.loads(path.read_text(encoding="utf-8")))
    by_id = {str(r["id"]): r for r in data.get("records") or []}
    by_id.update({str(k): v for k, v in updated.items()})
    data["records"] = list(by_id.values())
    data["counts"] = {"total": len(data["records"])}
    if tax:
        data["taxonomy"] = tax
    data["refreshed"] = _now()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def _by_doc(path: pathlib.Path) -> dict:
    out = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            c = json.loads(line)
            out.setdefault(c["doc_path"], []).append(c["text"])
    return {k: "\n".join(v) for k, v in out.items()}


def _critical_diff(old_docs: dict, new_docs: dict) -> list[dict]:
    """Which phone numbers, fees and emails would this update change?

    The whole reason an update is reviewed rather than applied. A page edit is
    information; a changed registration hotline is a decision, and the two must
    not be reported at the same volume.
    """
    alarms = []
    for key in sorted(set(new_docs) - set(old_docs)):
        for label, pat in CRITICAL.items():
            found = sorted(set(pat.findall(new_docs[key])))
            if found:
                alarms.append({"doc": key, "kind": label, "change": "new",
                               "added": found, "removed": []})
    for key in sorted(set(old_docs) - set(new_docs)):
        for label, pat in CRITICAL.items():
            found = sorted(set(pat.findall(old_docs[key])))
            if found:
                alarms.append({"doc": key, "kind": label, "change": "lost",
                               "added": [], "removed": found})
    for key in sorted(k for k in new_docs
                      if k in old_docs and new_docs[k] != old_docs[k]):
        for label, pat in CRITICAL.items():
            was = set(pat.findall(old_docs[key]))
            now = set(pat.findall(new_docs[key]))
            if was != now:
                alarms.append({"doc": key, "kind": label, "change": "edited",
                               "added": sorted(now - was),
                               "removed": sorted(was - now)})
    return alarms


def stage(progress, budget: int = 200, interval: float | None = None) -> dict:
    """Pull the changed pages, rebuild into staging, and report what would move.

    Nothing the app answers from is touched. The live corpus and index stay
    exactly as they are until someone presses Apply.
    """
    _deps()
    state = load_state()
    pending = state.get("pending") or {}
    ids = list(pending.get("ids") or [])
    live = pending.get("live") or {}
    if not ids:
        raise Unavailable("Nothing to update. Check for updates first.")

    arc = root() / NEW_SUBDIR
    arc.mkdir(parents=True, exist_ok=True)

    f = _fetcher(max(budget, len(ids) + 20), interval)
    tax = taxonomies(f)
    updated, done, fetched = {}, 0, []
    for wid in ids:
        rec = live.get(wid)
        if not rec:
            done += 1
            continue
        progress({"stage": "fetching", "count": done, "total": len(ids),
                  "detail": rec["title"][:60]})
        url = (f"{API}/{rec.get('rest_base', 'pages')}/{wid}"
               f"?_fields=id,link,slug,date,modified,title,content,"
               f"topics,categories,audiences")
        try:
            item = f.get_json(url, conditional=False)
        except scrape.Refused:
            raise          # budget/robots stop the whole run, loudly -- see _paginate
        except Exception:                                      # noqa: BLE001
            item = None
        done += 1
        if not item or not (item.get("content") or {}).get("rendered"):
            continue
        relfile, text = _write_doc(arc, item, rec)
        updated[wid] = _record(item, rec, relfile, text)
        fetched.append({"id": wid, "title": rec["title"], "url": rec["url"]})
    f.save_cache()

    if not updated:
        raise Unavailable(
            "The changed pages could not be downloaded. Try again in a moment."
        )

    _sync_index(arc, updated, tax)

    # Build into staging, never over the top of what is being read right now.
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    progress({"stage": "chunking"})
    from . import corpus, index
    kb = corpus.build(root=root(), out_dir=STAGE_DIR, log=lambda *a: None)

    def relay(stage_name, count, total):
        progress({"stage": stage_name, "count": count, "total": total})

    index.build(out_dir=STAGE_DIR / "index", corpus=STAGE_DIR / "corpus.jsonl",
                progress=relay)

    report = {
        "fetched": fetched,
        "chunks": kb["total_chunks"],
        "chunks_before": _count_lines(config.CORPUS),
        "critical": _critical_diff(_by_doc(config.CORPUS),
                                   _by_doc(STAGE_DIR / "corpus.jsonl")),
        "http": f.report(),
    }
    state = load_state()
    state.setdefault("pending", {}).update(
        {"fetched": fetched, "built": _today(), "report": report})
    save_state(state)
    return report


def _count_lines(path: pathlib.Path) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


# --------------------------------------------------------------------- apply
def apply(progress=None) -> dict:
    """Swap staging in atomically, keeping one rollback copy.

    Identical in effect to `pipeline/refresh.py promote`, including the backup,
    so an operator who applies an update from the window and one who promotes
    from a terminal end up in the same state.
    """
    stage_corpus = STAGE_DIR / "corpus.jsonl"
    stage_index = STAGE_DIR / "index"
    if not (stage_corpus.exists() and (stage_index / "meta.json").exists()):
        raise Unavailable("There is no downloaded update to apply.")

    if progress:
        progress({"stage": "installing"})

    backup = config.DATA_DIR / "previous"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    if config.CORPUS.exists():
        shutil.copy2(config.CORPUS, backup / "corpus.jsonl")
    if config.INDEX_DIR.exists():
        shutil.copytree(config.INDEX_DIR, backup / "index")

    tmp = config.DATA_DIR / "index.incoming"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(stage_index, tmp)
    retiring = config.DATA_DIR / "index.retiring"
    if retiring.exists():
        shutil.rmtree(retiring)
    if config.INDEX_DIR.exists():
        config.INDEX_DIR.rename(retiring)
    try:
        tmp.rename(config.INDEX_DIR)
    except OSError:
        if retiring.exists() and not config.INDEX_DIR.exists():
            retiring.rename(config.INDEX_DIR)     # put the old one back
        raise
    if retiring.exists():
        shutil.rmtree(retiring)
    shutil.copy2(stage_corpus, config.CORPUS)
    # The manifest is copied too. pipeline/refresh.py's promote does not, and
    # preflight.py has a whole branch apologising for the stale date that
    # leaves behind -- copying it is cheaper than explaining it.
    if (STAGE_DIR / "kb_manifest.json").exists():
        shutil.copy2(STAGE_DIR / "kb_manifest.json",
                     config.DATA_DIR / "kb_manifest.json")

    state = load_state()
    pending = state.get("pending") or {}
    if pending.get("live"):
        state["baseline"], state["captured"] = pending["live"], _today()
    state.setdefault("history", []).append(
        {"promoted": _today(), "at": _now(), "ids": pending.get("ids", []),
         "documents": len(pending.get("fetched") or [])})
    state["pending"] = None
    save_state(state)

    shutil.rmtree(STAGE_DIR, ignore_errors=True)

    # retrieve.py caches the index in memory on first use, so without this the
    # running app keeps answering from the archive it loaded at startup and the
    # update appears to have done nothing at all.
    from . import retrieve
    retrieve.reset()

    return {"chunks": _count_lines(config.CORPUS),
            "rollback": str(backup)}


def discard() -> dict:
    """Throw away a downloaded update without applying it.

    The refreshed page files stay on disk -- they are the archive's own copy of
    what the site says today, and deleting them would mean re-downloading them
    on the next attempt for no gain. Only the built corpus and index are
    dropped, so nothing half-reviewed can be applied by accident later.
    """
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    state = load_state()
    pending = state.get("pending")
    if pending:
        pending.pop("built", None)
        pending.pop("report", None)
        save_state(state)
    return {"ok": True}


# -------------------------------------------------------------------- status
def _bundle_status() -> dict:
    """Never let a reporting call take the panel down."""
    try:
        from . import bundle
        return bundle.describe()
    except Exception:                                          # noqa: BLE001
        return {"configured": False}


def status() -> dict:
    """Everything the archive panel shows. Local, read-only, instant."""
    state = load_state()
    manifest = {}
    try:
        manifest = json.loads(
            (config.DATA_DIR / "kb_manifest.json").read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        manifest = {}

    idx_path = root() / ARCHIVE_INDEX
    documents, media, captured_at = None, None, None
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        documents = (idx.get("counts") or {}).get("total") or len(idx.get("records") or [])
        media = len(idx.get("media") or [])
        captured_at = idx.get("refreshed") or idx.get("captured")
    except Exception:                                          # noqa: BLE001
        pass

    pending = state.get("pending") or {}
    history = state.get("history") if isinstance(state.get("history"), list) else []
    last = history[-1] if history else {}
    staged = ((STAGE_DIR / "corpus.jsonl").exists()
              and (STAGE_DIR / "index" / "meta.json").exists())

    # Titles, not just a count, so a person deciding whether to build an
    # update can see WHAT changed ("Registration hotline" is worth reading
    # before pressing anything) rather than trusting a bare number.
    live = pending.get("live") or {}
    pending_titles = [
        {"id": wid, "title": live[wid]["title"], "url": live[wid]["url"]}
        for wid in (pending.get("ids") or []) if wid in live
    ][:30]

    return {
        "has_archive": config.CORPUS.exists(),
        "can_crawl": True,
        "site": SITE,
        "archive_root": str(root()),
        "documents": documents,
        "media": media,
        "captured": captured_at or state.get("captured"),
        "built": manifest.get("built"),
        "chunks": manifest.get("total_chunks") or _count_lines(config.CORPUS),
        "by_source": manifest.get("chunks_by_source") or {},
        "by_topic": manifest.get("chunks_by_topic") or {},
        "last_checked": state.get("last_checked"),
        "last_applied": last.get("at") or last.get("promoted"),
        "pending_ids": list(pending.get("ids") or []),
        "pending_detected": pending.get("detected"),
        "pending_titles": pending_titles,
        "staged": staged,
        "staged_report": pending.get("report") if staged else None,
        # Where the un-crawlable half would come from, if an operator has
        # configured a source. Host and flags only -- never the URL, which for
        # a private archive can carry a signed credential in its query string.
        "bundle": _bundle_status(),
        "credit": "Guidance published by UNHCR and its partners.",
        "not_affiliated": (
            "This is an independent tool. It is not affiliated with, endorsed "
            "by, or operated by UNHCR."
        ),
    }
