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

What is NOT here, and why:

    installing Ollama
                    a second application, needing an administrator. Offering a
                    fake "install" button that silently fails is worse than
                    linking the download.

Every function takes a `progress` callback and reports through it. These steps
take minutes, and a progress bar is the difference between "working" and
"frozen" to the person waiting.
"""
import json
import pathlib
import shutil
import threading
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
