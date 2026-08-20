"""
Fetching the part of the archive no crawl can reach.

The live site is recoverable: pakpatat/archive.py crawls it, and a fresh
install can rebuild that half from nothing. The other half cannot be recovered
by anyone, ever. refugeemalaysia.org was taken down on 2026-07-14, and the 80
passages it left behind -- the 22-clinic NGO directory with addresses and
phone numbers, the Verify Plus explainer, the refugee lexicon -- exist only in
captures individual operators happen to hold. Partner materials were never
published anywhere at all.

Until now the only way to move those to a new machine was to hand someone a
folder. This module is that handoff, automated: one archive bundle at a URL the
operator controls, fetched as the first step of "Get the archive".

DELIBERATELY UNCONFIGURED BY DEFAULT
------------------------------------
There is no built-in URL and there will not be one. The material in a bundle is
UNHCR's copyrighted work (NOTICE.md), and publishing it is UNHCR's decision to
make, not this project's -- so the software ships the mechanism and the operator
supplies a source they have the right to serve. An install with nothing
configured simply skips this step and crawls the live site, which is exactly
what it did before.

    PAKPATAT_ARCHIVE_BUNDLE   https:// URL of a .tar.gz or .zip
    PAKPATAT_ARCHIVE_TOKEN    sent as `Authorization: Bearer ...` (optional)
    PAKPATAT_ARCHIVE_SHA256   expected digest, if you want it pinned (optional)

Point it at a private release asset, an S3 object, or anything else that
answers an authenticated GET.

WHAT THIS CODE ASSUMES ABOUT THE FILE IT DOWNLOADS: nothing.
A bundle is an archive from the network, and this extracts it onto a case
worker's machine. Every guard below exists because the honest threat model for
"download an archive and unpack it" includes the archive being hostile -- a
wrong URL, a compromised host, a token pasted into the wrong field.
"""
import hashlib
import os
import pathlib
import re
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import config

# A bundle is page text, a handful of PDFs and some posters. The largest real
# one measured is ~56MB. The cap is generous against that and still refuses to
# fill a laptop's disk because a URL pointed at something enormous.
MAX_BYTES = int(os.getenv("PAKPATAT_ARCHIVE_MAX_BYTES", str(2 * 1024 ** 3)))

# Refuse a bundle that expands to absurdly more than it downloads. A zip bomb
# is a small file that is not small once written, and the process doing the
# writing here is an app a case worker left open.
MAX_EXPANSION = 200

CHUNK = 256 * 1024


class Unavailable(RuntimeError):
    """The bundle could not be fetched or trusted, with the reason why."""


# Share links that do not serve the file. Pasting the address you see in a
# browser is the obvious thing to do and, for every one of these hosts, it
# returns an HTML page rather than an archive -- so the paste is normalised here
# instead of being documented as a gotcha nobody reads.
#
# Google Drive is the one worth knowing about in detail: a /file/d/<id>/view URL
# is a viewer page and the download lives at a different endpoint, which the
# rewrite below handles. What it does NOT handle is Drive's virus-scan
# interstitial, which large files (~100MB+) answer the first request with
# instead of bytes. That is deliberate: clicking through it programmatically
# means scraping a token out of Google's HTML and re-requesting, which breaks
# whenever they change the page, and the recommended bundle is ~3.6MB and never
# trips it (scripts/make-archive-bundle.command). A bundle big enough to hit it
# gets a plain explanation from _explain_not_an_archive() and a suggestion to
# host it somewhere that serves files, rather than a silent corrupt download.
_REWRITES = (
    # drive.google.com/file/d/<id>/view  ->  direct download
    (re.compile(r"^https://drive\.google\.com/file/d/([^/]+)"),
     lambda m: f"https://drive.google.com/uc?export=download&id={m.group(1)}"),
    # drive.google.com/open?id=<id>
    (re.compile(r"^https://drive\.google\.com/open\?id=([^&]+)"),
     lambda m: f"https://drive.google.com/uc?export=download&id={m.group(1)}"),
    # Dropbox: dl=0 is the preview page
    (re.compile(r"^(https://www\.dropbox\.com/[^?]+)\?.*$"),
     lambda m: f"{m.group(1)}?dl=1"),
    # GitHub blob page -> raw bytes
    (re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$"),
     lambda m: f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"),
)


def normalize(target: str) -> str:
    """Turn a human-facing share link into one that returns the actual file."""
    for pattern, rewrite in _REWRITES:
        m = pattern.match(target)
        if m:
            return rewrite(m)
    return target


def url() -> str | None:
    raw = os.getenv("PAKPATAT_ARCHIVE_BUNDLE") or None
    return normalize(raw) if raw else None


def token() -> str | None:
    return os.getenv("PAKPATAT_ARCHIVE_TOKEN") or None


def expected_sha256() -> str | None:
    v = (os.getenv("PAKPATAT_ARCHIVE_SHA256") or "").strip().lower()
    return v or None


def configured() -> bool:
    return bool(url())


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the token to a different host.

    urllib re-sends every header it was given on a redirect, including
    Authorization. A URL that redirects off-host would therefore hand the
    operator's credential to whoever the new host is -- which is how a private
    archive's token ends up somewhere it was never meant to go. Same host:
    fine. Anywhere else: the header is dropped and the request continues
    without it, so a public mirror still works and a private one fails
    honestly instead of leaking.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise Unavailable(
                f"The archive URL redirected to a non-HTTPS address ({newurl}). "
                "Refusing to continue."
            )
        if urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(
                req.full_url).netloc:
            new.remove_header("Authorization")
        return new


def _open(target: str):
    if urllib.parse.urlparse(target).scheme != "https":
        raise Unavailable(
            "The archive bundle URL must start with https:// -- refusing to "
            "download an archive over an unencrypted connection."
        )
    headers = {"User-Agent": f"Pakpatat/{_version()} (archive bundle fetch)"}
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    opener = urllib.request.build_opener(_SameHostRedirect())
    return opener.open(urllib.request.Request(target, headers=headers), timeout=60)


def _version() -> str:
    from . import __version__
    return __version__


# The first bytes of the formats a bundle may legitimately be. Checked instead
# of trusting Content-Type, because the single most likely wrong answer here --
# a Google Drive interstitial, a login page, an S3 access-denied XML document --
# arrives with a perfectly ordinary 200 and a believable content type. Sniffing
# turns "the archive is corrupt" into "that link served a web page, not a file".
_MAGIC = {
    b"\x1f\x8b": "gzip",          # .tar.gz / .tgz
    b"PK\x03\x04": "zip",
    b"PK\x05\x06": "zip",         # empty zip
    b"BZh": "bzip2",
    b"\xfd7zXZ": "xz",
    b"ustar": "tar",              # offset 257, handled separately below
}


def _sniff(head: bytes) -> str | None:
    for magic, kind in _MAGIC.items():
        if magic != b"ustar" and head.startswith(magic):
            return kind
    if head[257:262] == b"ustar":
        return "tar"
    return None


def _explain_not_an_archive(head: bytes, target: str) -> str:
    """Say what actually came back, in terms the person can act on."""
    text = head[:400].decode("utf-8", "replace").lower()
    host = urllib.parse.urlparse(target).netloc

    if "drive.google.com" in host or "googleusercontent" in host:
        if "virus" in text or "scan" in text or "confirm" in text:
            return (
                "Google Drive returned its virus-scan warning page instead of "
                "the file, which it does for large files. Use a host that "
                "serves the file directly, or share it as a published release "
                "asset instead."
            )
        if "sign in" in text or "accounts.google" in text:
            return (
                "Google Drive asked this computer to sign in, so the link is "
                "not readable without a browser. A Drive link only works here "
                "if it is shared as 'anyone with the link' -- and note that "
                "means anyone who obtains the link, which is not the same as "
                "private."
            )
        return (
            "Google Drive returned a web page rather than the file. Check the "
            "link is a file share, not a folder -- a folder cannot be "
            "downloaded as one archive."
        )
    if text.lstrip().startswith(("<!doctype", "<html")):
        return (
            "That address returned a web page, not a file. Paste the direct "
            "download link rather than the page you view it on."
        )
    if "<error" in text or "accessdenied" in text:
        return (
            "The storage host refused access to that object. Check the link "
            "has not expired and that PAKPATAT_ARCHIVE_TOKEN is set if it "
            "needs one."
        )
    return (
        "That address did not return a .tar.gz or .zip archive. Check the link "
        "points straight at the bundle file."
    )


def _download(progress) -> tuple[pathlib.Path, str]:
    """Stream the bundle to a temp file, returning (path, sha256).

    Streamed rather than read into memory: these run to tens of megabytes and
    the machine this is aimed at has 8GB and an answering model already resident
    in it.
    """
    target = url()
    try:
        resp = _open(target)
    except Unavailable:
        raise
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Unavailable(
                "The archive source refused this computer "
                f"(HTTP {e.code}). Check PAKPATAT_ARCHIVE_TOKEN."
            ) from e
        if e.code == 404:
            raise Unavailable(
                "The archive bundle was not found at that address (HTTP 404)."
            ) from e
        raise Unavailable(f"Could not download the archive (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise Unavailable(f"Could not reach the archive source ({e.reason}).") from e

    total = resp.headers.get("Content-Length")
    total = int(total) if (total or "").isdigit() else None
    if total and total > MAX_BYTES:
        resp.close()
        raise Unavailable(
            f"The archive bundle is {total / 1024**2:.0f}MB, over the "
            f"{MAX_BYTES / 1024**2:.0f}MB limit. Refusing to download it."
        )

    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(prefix="pakpatat-bundle-", suffix=".dl")
    tmp = pathlib.Path(tmp_name)
    seen = 0
    head = b""
    try:
        with os.fdopen(fd, "wb") as out, resp:
            while True:
                buf = resp.read(CHUNK)
                if not buf:
                    break
                # Decide whether this is an archive at all from the first
                # block, before spending someone's data allowance pulling down
                # 46MB of an HTML error page.
                if not head:
                    head = buf[:512]
                    if _sniff(head) is None:
                        raise Unavailable(_explain_not_an_archive(head, target))
                seen += len(buf)
                # Checked as it arrives, not only from Content-Length: a server
                # is free to under-report or omit it entirely, and by the time
                # a missing header is noticed the disk is already full.
                if seen > MAX_BYTES:
                    raise Unavailable(
                        "The archive bundle exceeded the size limit while "
                        "downloading. Refusing to continue."
                    )
                digest.update(buf)
                out.write(buf)
                progress({"stage": "bundle", "count": seen, "total": total})
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, digest.hexdigest()


def _verify(actual: str) -> None:
    want = expected_sha256()
    if want and actual != want:
        raise Unavailable(
            "The downloaded archive does not match the expected checksum.\n"
            f"  expected {want}\n  got      {actual}\n"
            "It was NOT unpacked. Either the file changed or it is not the "
            "one you meant to serve."
        )


def _safe_members(names, dest: pathlib.Path):
    """Reject any entry that would write outside `dest`.

    An archive controls its own member names, so "../../.ssh/authorized_keys"
    is a path a malicious bundle is free to ask for. tarfile's data filter
    covers this on Python 3.12+, but this is checked here too and for zips,
    because the consequence of getting it wrong is arbitrary file write on
    someone else's laptop.
    """
    root = dest.resolve()
    for name in names:
        if name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts:
            raise Unavailable(
                f"The archive contains an unsafe path ({name!r}) and was not "
                "unpacked."
            )
        if not str((dest / name).resolve()).startswith(str(root)):
            raise Unavailable(
                f"The archive contains an entry that escapes the archive "
                f"folder ({name!r}) and was not unpacked."
            )


def _extract(path: pathlib.Path, downloaded: int, dest: pathlib.Path,
             progress) -> int:
    """Unpack into `dest`, returning the number of files written."""
    dest.mkdir(parents=True, exist_ok=True)
    progress({"stage": "unpacking"})

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            _safe_members(names, dest)
            expanded = sum(i.file_size for i in z.infolist())
            _check_expansion(expanded, downloaded)
            z.extractall(dest)
            return len(names)

    try:
        with tarfile.open(path) as t:
            names = t.getnames()
            _safe_members(names, dest)
            expanded = sum(m.size for m in t.getmembers())
            _check_expansion(expanded, downloaded)
            try:
                # Python 3.12+: refuses absolute paths, traversal, links out of
                # the tree, device nodes and setuid bits. Belt as well as the
                # braces above.
                t.extractall(dest, filter="data")
            except TypeError:
                t.extractall(dest)
            return len(names)
    except tarfile.TarError as e:
        raise Unavailable(
            "The downloaded file is not a readable .tar.gz or .zip archive."
        ) from e


def _check_expansion(expanded: int, downloaded: int) -> None:
    if expanded > MAX_BYTES:
        raise Unavailable(
            f"The archive expands to {expanded / 1024**2:.0f}MB, over the "
            f"{MAX_BYTES / 1024**2:.0f}MB limit. It was not unpacked."
        )
    if downloaded and expanded > downloaded * MAX_EXPANSION:
        raise Unavailable(
            "The archive expands to far more than it downloads "
            f"({expanded / max(downloaded, 1):.0f}x). It was not unpacked."
        )


# What a bundle is expected to contain, and what each part is worth. Keyed by
# the directory pakpatat/corpus.py actually reads, so this list cannot drift
# from what the corpus builder looks for without the check going quiet.
LAYOUT = (
    ("01_support_topics", "retired-site capture",
     "the NGO clinic directory, Verify Plus and the refugee lexicon -- "
     "unrecoverable if lost, refugeemalaysia.org is gone"),
    ("07_partner_materials", "partner materials",
     "material handed over directly and never published anywhere"),
    ("05_intelligence/gap_analysis", "gap analysis",
     "what the live site dropped -- without it, retired pages cannot be "
     "ranked behind their live replacements"),
    ("04_help_unhcr_2026", "live-site capture",
     "optional in a bundle: the app can crawl this for itself"),
)


def inspect(dest: pathlib.Path | None = None) -> dict:
    """What is actually in the archive folder, part by part.

    Run after unpacking, and worth running on its own. A bundle can be a
    perfectly valid .tar.gz and still be the wrong thing -- someone's home
    directory, last year's copy, an archive rooted one level too deep -- and
    every one of those unpacks without error and produces an app that answers
    "that is not in the archive" to everything. Better to say which parts
    arrived than to report a byte count and let the corpus build come up empty.
    """
    dest = dest or (config.ARCHIVE_ROOT or (config.DATA_DIR / "archive"))
    parts = []
    for rel, label, why in LAYOUT:
        d = dest / rel
        files = sum(1 for _ in d.rglob("*")) if d.is_dir() else 0
        parts.append({"path": rel, "label": label, "why": why,
                      "present": d.is_dir(), "files": files})
    usable = any(p["present"] for p in parts)
    return {"root": str(dest), "parts": parts, "usable": usable}


def _check_layout(dest: pathlib.Path) -> dict:
    found = inspect(dest)
    if not found["usable"]:
        # Nested-root is the overwhelmingly common mistake -- zipping the
        # folder rather than its contents -- so name it rather than leaving the
        # operator to guess what "unrecognised" means.
        nested = [p.name for p in dest.iterdir()
                  if p.is_dir() and (p / "01_support_topics").is_dir()]
        hint = (f" It looks like everything sits inside a '{nested[0]}/' folder "
                f"-- rebuild the bundle from inside that folder, not around it."
                if nested else
                " Expected a folder containing 01_support_topics/ and/or "
                "07_partner_materials/ at its top level.")
        raise Unavailable(
            "The download unpacked, but nothing in it looks like a "
            "Päkpätät archive." + hint
        )
    return found


def fetch(progress, dest: pathlib.Path | None = None) -> dict:
    """Download and unpack the configured bundle. Raises Unavailable on refusal.

    Unpacked over whatever is already there, deliberately: a bundle is how an
    operator distributes corrections, and a second run should apply them rather
    than silently keep the older copy. Nothing is deleted that the bundle does
    not itself replace.
    """
    if not configured():
        raise Unavailable("No archive bundle is configured on this computer.")

    dest = dest or (config.ARCHIVE_ROOT or (config.DATA_DIR / "archive"))
    tmp, digest = _download(progress)
    try:
        _verify(digest)
        files = _extract(tmp, tmp.stat().st_size, dest, progress)
    finally:
        tmp.unlink(missing_ok=True)

    found = _check_layout(dest)
    return {"files": files, "sha256": digest,
            # Host, not the full URL: a signed link carries a credential in its
            # query string and this value is reported back into the UI.
            "source": urllib.parse.urlparse(url()).netloc,
            "verified": bool(expected_sha256()),
            "parts": [p["label"] for p in found["parts"] if p["present"]]}


def describe() -> dict:
    """What the Knowledge base panel shows about the configured source."""
    target = url()
    return {
        "configured": bool(target),
        # Host only. The full URL of a private archive can carry a signed query
        # string, and this string is rendered into a panel someone may well
        # screenshot when asking for help.
        "host": urllib.parse.urlparse(target).netloc if target else None,
        "authenticated": bool(token()),
        "pinned": bool(expected_sha256()),
    }
