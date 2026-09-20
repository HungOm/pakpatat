# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
The setup work the app can do for itself, instead of printing a command.

preflight.py REPORTS what is missing and never touches anything -- that
separation is deliberate and stays. This module is the other half: the
pieces a running app can actually fix on its own, with the user's consent,
from a button.

    rebuild_index   embed data/corpus.jsonl into data/index/. Everything it
                    needs is already on the machine (or bundled in the app),
                    so it works with the wifi off.
    pull_model      download the local answering model through Ollama. The one
                    genuinely large first-run download, and the only step here
                    that needs internet.
    crawl_archive   first copy: crawl help.unhcr.org into a fresh archive, for
                    an install that has none of its own. See pakpatat/archive.py.
    check_updates   ask the live site what changed since the last capture --
                    a few seconds, no download.
    stage_update    fetch the changed pages and build them into a staging copy,
                    without touching what the app answers from.
    apply_update    swap the staged build in, atomically, keeping one rollback.
    discard_update  throw away a staged build nobody applied.

    install_ollama  fetch Ollama and install it, per-user, with no password.
                    Windows and macOS. Linux still links out -- see below.

What is NOT here, and why:

    installing Ollama on Linux
                    This entry used to cover every platform and said "a second
                    application, needing an administrator". That was worth
                    checking rather than believing, and it was wrong twice.

                    Windows: Ollama ships an Inno Setup package with
                    PrivilegesRequired=lowest, installing per-user into
                    {localappdata}\\Programs\\Ollama. No password, nothing
                    system-wide.

                    macOS: Ollama-darwin.zip is a .app, and ollama.py already
                    looks for one in ~/Applications and already knows how to
                    start it (see _CANDIDATES and _spawn). Unzipping into a
                    folder inside the user's own home needs no password
                    either, and at ~190MB it is a fraction of the Windows
                    download, which carries GPU runtimes macOS does not need.

                    Linux is the one that really does install through a shell
                    script piped to root, so it keeps the link, and the
                    original warning stands for it: a fake "install" button
                    that silently fails is worse than an honest one.

Every function takes a `progress` callback and reports through it. These steps
take minutes, and a progress bar is the difference between "working" and
"frozen" to the person waiting.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import config

# One at a time. Two concurrent index builds would race on the same staging
# directory and the final swap, and the honest answer to a second click is
# "this is already running", not a corrupted index.
_lock = threading.Lock()


class Busy(Exception):
    """Another setup step is already running."""


class Unavailable(Exception):
    """This step cannot run on this machine right now, with the reason why."""


# ------------------------------------------------------------------- index
def _swap_in(built: pathlib.Path) -> None:
    """Move a finished build into place without a half-written moment.

    A directory rename is atomic on POSIX and near enough on Windows, so a
    reader sees either the whole old index or the whole new one. Copying files
    in place instead would let a question land mid-write -- and pakpatat/graph
    would answer from an index that is half one archive and half another.
    """
    live = config.INDEX_DIR
    retiring = config.DATA_DIR / "index.replacing"
    if retiring.exists():
        shutil.rmtree(retiring)
    if live.exists():
        live.rename(retiring)
    try:
        built.rename(live)
    except OSError:
        # Put the old one back rather than leaving the app with no index at all.
        if retiring.exists() and not live.exists():
            retiring.rename(live)
        raise
    if retiring.exists():
        shutil.rmtree(retiring)


def rebuild_index(progress) -> dict:
    """Embed the corpus into a fresh index and swap it in.

    Builds into a staging directory first for the reason in _swap_in, and
    clears retrieve's in-memory cache at the end -- without that last step the
    running app keeps answering from the index it loaded at startup, and the
    button would appear to have done nothing.
    """
    if not config.CORPUS.exists():
        raise Unavailable(
            "There is no archive on this computer to index yet."
        )

    from . import index, retrieve

    building = config.DATA_DIR / "index.building"
    if building.exists():
        shutil.rmtree(building)

    # `count`, never `done`: the stream this ends up on marks its final message
    # with done=true, and a progress event carrying done=0 would read as
    # "finished" to any client that tests it loosely.
    def relay(stage, count, total):
        progress({"stage": stage, "count": count, "total": total})

    index.build(out_dir=building, progress=relay)
    progress({"stage": "installing"})
    _swap_in(building)
    retrieve.reset()

    meta = json.loads(config.INDEX_META.read_text(encoding="utf-8"))
    return {"chunks": len(meta.get("chunks", []))}


# ------------------------------------------------------------------- model
def pull_model(progress) -> dict:
    """Download the local answering model through Ollama, reporting bytes.

    Ollama's /api/pull streams NDJSON with a running byte count, which is the
    only reason this is worth doing in-app at all: `ollama pull` in a terminal
    the user does not have open cannot show them a 2GB download is moving.
    """
    from . import ollama

    up, msg = ollama.ensure(timeout=45.0)
    if not up:
        raise Unavailable(msg)

    name = config.MODEL_NAME
    body = json.dumps({"model": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama.host()}/api/pull", data=body,
        headers={"Content-Type": "application/json"},
    )

    last = ""
    try:
        # No overall timeout: a 2GB download on a slow connection is normal
        # here. The read blocks between chunks, and Ollama sends status lines
        # steadily, so a genuinely dead connection still raises.
        with urllib.request.urlopen(req) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise Unavailable(str(ev["error"]))
                last = ev.get("status") or last
                progress({"stage": "downloading", "detail": last,
                          "count": ev.get("completed"), "total": ev.get("total")})
    except urllib.error.URLError as e:
        raise Unavailable(
            f"Could not reach the local AI engine to download the model ({e.reason})."
        ) from e

    if not ollama.has_model(name):
        raise Unavailable(
            f"The download finished but '{name}' is still not installed. "
            "Try again, or check there is enough free disk space."
        )
    return {"model": name}


# What to fetch, and what the first bytes of it must look like. The size is
# in the UI text, not here, but for the record: Windows is ~1.5GB because it
# bundles CUDA and ROCm; macOS is ~190MB because Metal is already in the OS.
#
# One URL each covers both architectures -- Ollama's own installer declares
# ArchitecturesAllowed=x64compatible arm64, and the mac .app is universal.
OLLAMA_DOWNLOAD = {
    "win32": ("https://ollama.com/download/OllamaSetup.exe",
              "OllamaSetup.exe", b"MZ", "a Windows program"),
    "darwin": ("https://ollama.com/download/Ollama-darwin.zip",
               "Ollama-darwin.zip", b"PK\x03\x04", "a zip archive"),
}

# Generous rather than tight: this is here to refuse something absurd -- a
# captive-portal page, a redirect to the wrong file -- not to police Ollama's
# release size.
OLLAMA_MAX_BYTES = 3 * 1024 ** 3


def _fetch_ollama(url: str, dest: pathlib.Path, magic: bytes, shape: str,
                  progress) -> None:
    """Download `url` to `dest`, reporting bytes, and prove it is what it claims.

    The file is about to be executed or unpacked, so "did it arrive" is not
    the question -- "is it the thing" is. A login page from a campus firewall
    is a perfectly successful HTTP response.
    """
    from . import bundle

    # bundle.py's redirect guard, reused rather than re-written: it refuses a
    # redirect that leaves https, which is the whole risk in fetching
    # something we are going to run. There is no token here for it to strip.
    opener = urllib.request.build_opener(bundle._SameHostRedirect())
    req = urllib.request.Request(
        url, headers={"User-Agent": "Pakpatat (Ollama setup fetch)"})
    try:
        resp = opener.open(req, timeout=60)
    except urllib.error.URLError as e:
        raise Unavailable(
            f"Could not reach ollama.com ({getattr(e, 'reason', e)}). "
            "Install it from ollama.com/download instead."
        ) from e

    with resp:
        total = int(resp.headers.get("Content-Length") or 0)
        if total and total > OLLAMA_MAX_BYTES:
            raise Unavailable(
                "That download is far larger than Ollama should be, so it "
                "was refused. Install it from ollama.com/download instead.")
        got = 0
        with dest.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > OLLAMA_MAX_BYTES:
                    raise Unavailable(
                        "The download kept going past the size Ollama should "
                        "be, so it was stopped.")
                fh.write(chunk)
                progress({"stage": "downloading", "detail": "Ollama",
                          "count": got, "total": total or None})

    with dest.open("rb") as fh:
        if not fh.read(len(magic)).startswith(magic):
            raise Unavailable(
                f"What came back from ollama.com was not {shape}. If this "
                "computer is behind a login page or a company firewall, that "
                "is the usual cause. Install it from ollama.com/download "
                "instead.")


def install_ollama(progress) -> dict:
    """Install Ollama, per-user, with no administrator. Windows and macOS.

    The download reports bytes exactly like pull_model: the alternative is a
    button that looks frozen for several minutes.

    Every failure path ends by naming the download page, because the one thing
    worse than not having this button is having one that fails quietly and
    leaves the user with no idea what to do next.
    """
    from . import ollama

    # Asked before anything else, and on every platform including the ones
    # that cannot install: "you already have this" is the right answer to a
    # Linux user pressing a button too, not an unsupported-platform error.
    if ollama.executable():
        return {"already_installed": True}

    spec = OLLAMA_DOWNLOAD.get(sys.platform)
    if spec is None:
        raise Unavailable(
            "Installing Ollama automatically is not supported on this system. "
            "Install it from ollama.com/download and reopen the app.")
    url, filename, magic, shape = spec

    progress({"stage": "installing", "detail": "Fetching Ollama"})
    work = pathlib.Path(tempfile.mkdtemp(prefix="pakpatat-ollama-"))
    try:
        payload = work / filename
        _fetch_ollama(url, payload, magic, shape, progress)

        if sys.platform == "win32":
            progress({"stage": "installing", "detail": "Running Ollama's installer"})
            # Inno Setup, PrivilegesRequired=lowest: per-user, no password,
            # and /VERYSILENT keeps it from putting a second wizard in front
            # of someone who already pressed a button in this one.
            proc = subprocess.run(
                [str(payload), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                capture_output=True, timeout=1800)
            if proc.returncode != 0:
                raise Unavailable(
                    f"Ollama's installer stopped with code {proc.returncode}. "
                    "Install it from ollama.com/download instead.")
        else:
            progress({"stage": "unpacking", "detail": "Installing Ollama"})
            # ditto, not zipfile. Python's ZipFile drops the executable bit,
            # which would unpack an Ollama.app that cannot run -- a failure
            # that would surface much later as "the engine will not start".
            # ditto is the tool macOS itself uses for app bundles.
            staged = work / "unpacked"
            staged.mkdir()
            proc = subprocess.run(["ditto", "-x", "-k", str(payload), str(staged)],
                                  capture_output=True, timeout=900)
            if proc.returncode != 0:
                raise Unavailable(
                    "Could not unpack Ollama "
                    f"({proc.stderr.decode('utf-8', 'replace').strip()[:120]}). "
                    "Install it from ollama.com/download instead.")

            # Take exactly the bundle we expect, from a directory we made and
            # control, rather than moving whatever the archive happened to
            # contain into the user's Applications folder.
            app = staged / "Ollama.app"
            if not app.is_dir():
                raise Unavailable(
                    "The download did not contain Ollama.app. Install it from "
                    "ollama.com/download instead.")

            target_dir = pathlib.Path("~/Applications").expanduser()
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / "Ollama.app"
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(app), str(target))

        # A successful install is not the same as a findable one: this
        # process inherited its environment before Ollama existed, so look
        # for the executable directly rather than trusting PATH.
        deadline = time.time() + 60
        while time.time() < deadline:
            if ollama.executable():
                break
            time.sleep(1.0)
        else:
            raise Unavailable(
                "Ollama installed but could not be found afterwards. "
                "Reopening the app usually picks it up.")

        progress({"stage": "installing", "detail": "Starting Ollama"})
        up, msg = ollama.ensure(timeout=60.0)
        if not up:
            raise Unavailable(msg)
        return {"installed": True, "path": ollama.executable()}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------- archive
# Thin wrappers: the crawling, diffing and atomic-swap logic lives in
# pakpatat/archive.py, alongside the CLI (pipeline/refresh.py) that does the
# same job from a terminal. This is only the mapping from a button name to a
# function, kept here so the dispatch table below stays the one place that
# lists everything the app can do to itself.
def crawl_archive(progress) -> dict:
    from . import archive
    try:
        return archive.capture(progress)
    except archive.Unavailable as e:
        raise Unavailable(str(e)) from e


def check_updates(progress) -> dict:
    from . import archive
    try:
        return archive.check(progress)
    except archive.Unavailable as e:
        raise Unavailable(str(e)) from e


def stage_update(progress) -> dict:
    from . import archive
    try:
        return archive.stage(progress)
    except archive.Unavailable as e:
        raise Unavailable(str(e)) from e


def apply_update(progress) -> dict:
    from . import archive
    try:
        return archive.apply(progress)
    except archive.Unavailable as e:
        raise Unavailable(str(e)) from e


def discard_update(progress) -> dict:
    from . import archive
    return archive.discard()


# ---------------------------------------------------------------- dispatch
ACTIONS = {
    "install_ollama": install_ollama,
    "rebuild_index": rebuild_index,
    "pull_model": pull_model,
    "crawl_archive": crawl_archive,
    "check_updates": check_updates,
    "stage_update": stage_update,
    "apply_update": apply_update,
    "discard_update": discard_update,
}


def run(action: str, progress) -> dict:
    """Run one named action. Raises Busy, Unavailable, or whatever it hit."""
    fn = ACTIONS.get(action)
    if fn is None:
        raise Unavailable(f"Unknown setup step '{action}'.")
    if not _lock.acquire(blocking=False):
        raise Busy("Another setup step is already running.")
    try:
        return fn(progress)
    finally:
        _lock.release()
