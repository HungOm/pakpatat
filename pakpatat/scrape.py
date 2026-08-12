"""
A deliberately slow, deliberately visible HTTP client for refreshing the archive.

This exists because the scrapers that built the archive lived outside the
repository, on one machine, behind hardcoded absolute paths -- so a fresh clone
had no way to get fresh data at all. It also exists because an UNATTENDED job
needs different manners from a human running a script once: nobody is watching
to notice that it has started hammering a humanitarian organisation's servers.

The goal is to be a light client, NOT a disguised one. Every measure here makes
the traffic smaller and more honest rather than harder to attribute:

  - robots.txt is fetched and obeyed (help.unhcr.org disallows only /wp-admin/,
    so the REST API this uses is permitted -- but that is checked, not assumed)
  - a real rate limit with jitter between every request
  - CONDITIONAL requests: once a page has been seen, later checks send
    If-None-Match / If-Modified-Since, so an unchanged page costs a 304 with no
    body. A nightly check of an unchanged site transfers almost nothing.
  - exponential backoff that honours Retry-After on 429/503
  - a User-Agent that says what this is and where to complain
  - a hard per-run request budget, so a bug in a loop cannot become a flood

NOTICE.md commits this project to respecting robots.txt and to not hammering
UNHCR's servers. This module is where that commitment is actually enforced,
rather than being a line in a document.

Standard library only: the nightly change check must run on a machine that has
installed nothing beyond the app's own requirements.
"""
import gzip
import json
import pathlib
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

# Identify the client honestly. A humanitarian site's admin who sees this in a
# log should be able to tell what it is and how to make it stop.
USER_AGENT = (
    "Pakpatat/1.0 (offline archive for a refugee community organisation; "
    "respects robots.txt; contact the operator via the CBO listed in NOTICE.md)"
)

# Seconds between requests. The site is a WordPress instance serving refugees;
# there is no deadline here worth a burst. Jitter avoids a machine-gun cadence
# and stops a nightly cron from hitting the exact same second every night.
MIN_INTERVAL = 2.0
JITTER = 0.75

# Give up rather than retry forever. A site that is down at 03:00 is better
# reported as "could not check" than retried for an hour.
MAX_RETRIES = 3
BACKOFF_BASE = 4.0
MAX_RETRY_AFTER = 120.0     # ignore an absurd Retry-After and just fail

# Fail-safe. `detect` needs ~2 requests and a full re-fetch ~60; anything past
# this is a bug in a loop, not a legitimate refresh.
DEFAULT_BUDGET = 200


class Refused(RuntimeError):
    """robots.txt forbids this URL, or the run exceeded its request budget."""


class SafeFetcher:
    """One instance per run. Holds the rate limiter, robots rules and budget."""

    def __init__(self, cache_path: pathlib.Path, budget: int = DEFAULT_BUDGET,
                 min_interval: float = MIN_INTERVAL, verbose: bool = True):
        self.cache_path = pathlib.Path(cache_path)
        self.budget = budget
        self.min_interval = min_interval
        self.verbose = verbose
        self.used = 0
        self._last_request = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._cache = self._load_cache()
        self.stats = {"requests": 0, "not_modified": 0, "fetched": 0, "retries": 0}

    # ---------------------------------------------------------------- caching
    def _load_cache(self) -> dict:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass          # a corrupt cache costs one full fetch, not a crash
        return {}

    def save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=2),
                                   encoding="utf-8")

    # ---------------------------------------------------------------- robots
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urllib.parse.urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{origin}/robots.txt")
            try:
                # Deliberately NOT rp.read(): that fetches with urllib's default
                # "Python-urllib/3.x" User-Agent, which help.unhcr.org rejects.
                # The parser then saw no rules, this module failed closed, and
                # a perfectly permitted API path was reported as disallowed.
                # Fetch it ourselves, with the same honest UA used everywhere
                # else, and hand the parser the text.
                self._wait()
                req = urllib.request.Request(f"{origin}/robots.txt",
                                             headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", "replace")
                self.stats["requests"] += 1
                rp.parse(body.splitlines())
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    rp.parse([])          # no robots.txt at all = nothing barred
                else:
                    rp.disallow_all = True
            except Exception:                                    # noqa: BLE001
                # Unreachable robots.txt is treated as "not allowed to guess".
                # Refusing to scrape is the safe direction for this project.
                rp.disallow_all = True
            self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(USER_AGENT, url)

    def crawl_delay(self, url: str) -> float:
        """Honour a site-declared Crawl-delay when it is SLOWER than ours."""
        try:
            d = self._robots_for(url).crawl_delay(USER_AGENT)
        except Exception:                                        # noqa: BLE001
            return self.min_interval
        return max(self.min_interval, float(d)) if d else self.min_interval

    # ------------------------------------------------------------ rate limit
    def _wait(self) -> None:
        gap = time.monotonic() - self._last_request
        target = self.min_interval + random.uniform(0, JITTER)
        if gap < target:
            time.sleep(target - gap)
        self._last_request = time.monotonic()

    # --------------------------------------------------------------- fetching
    def get(self, url: str, conditional: bool = True) -> tuple[int, bytes | None]:
        """
        Fetch `url`, returning (status, body).

        status 304 with body None means "unchanged since last time" -- the
        caller should skip it. That is the case this whole module optimises
        for: on a normal night nothing has changed and nothing is downloaded.
        """
        if self.used >= self.budget:
            raise Refused(f"request budget of {self.budget} exhausted -- "
                          f"refusing to continue (this is a bug guard)")
        if not self.allowed(url):
            raise Refused(f"robots.txt disallows {url}")

        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        entry = self._cache.get(url, {}) if conditional else {}
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]

        self.min_interval = self.crawl_delay(url)

        for attempt in range(MAX_RETRIES + 1):
            self._wait()
            self.used += 1
            self.stats["requests"] += 1
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = r.read()
                    # urllib does not decompress for us. We ask for gzip because
                    # a smaller transfer is the polite option, so we have to
                    # undo it here -- otherwise every response is binary noise.
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                        body = gzip.decompress(body)
                    self._remember(url, r.headers)
                    self.stats["fetched"] += 1
                    return r.status, body

            except urllib.error.HTTPError as e:
                if e.code == 304:
                    self.stats["not_modified"] += 1
                    return 304, None
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    delay = self._retry_delay(e, attempt)
                    self.stats["retries"] += 1
                    if self.verbose:
                        print(f"    {e.code} from server; backing off {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise

            except (urllib.error.URLError, OSError) as e:
                if attempt < MAX_RETRIES:
                    delay = BACKOFF_BASE * (2 ** attempt)
                    self.stats["retries"] += 1
                    if self.verbose:
                        print(f"    network error ({e}); retrying in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                raise

        raise RuntimeError("unreachable")

    def _retry_delay(self, err: urllib.error.HTTPError, attempt: int) -> float:
        """Prefer the server's own Retry-After; it knows better than we do."""
        ra = err.headers.get("Retry-After") if err.headers else None
        if ra:
            try:
                return min(float(ra), MAX_RETRY_AFTER)
            except ValueError:
                pass
        return BACKOFF_BASE * (2 ** attempt)

    def _remember(self, url: str, headers) -> None:
        etag = headers.get("ETag")
        lastmod = headers.get("Last-Modified")
        if etag or lastmod:
            self._cache[url] = {"etag": etag, "last_modified": lastmod}

    def get_json(self, url: str, conditional: bool = True):
        status, body = self.get(url, conditional=conditional)
        if status == 304 or body is None:
            return None
        return json.loads(body.decode("utf-8"))

    def report(self) -> str:
        s = self.stats
        return (f"{s['requests']} requests, {s['fetched']} fetched, "
                f"{s['not_modified']} unchanged (304), {s['retries']} retries")
