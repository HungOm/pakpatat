"""
Does this machine actually have everything it needs to answer offline?

The app's promise is that it works with the wifi off. That promise has five
separate ways to be false, and four of them used to fail *silently* -- the
window opened, looked perfectly healthy, and only produced "that is not in the
archive" when someone asked a real question. A missing index is
indistinguishable from an empty archive to the person typing.

So the splash asks all five before the window is usable, and shows the answers.
A case worker who can see WHICH piece is missing can act; one who sees a
working-looking app that refuses everything cannot.

Everything here REPORTS and never repairs. The steps the app can run for
itself live in pakpatat/firstrun.py, which this module only ever names
(`action`) so the splash can offer a button.

    from pakpatat import preflight
    report = preflight.run()
    report["ready"]          # bool -- can it answer right now?
    report["checks"]         # {key,label,ok,detail,fixable,fix,command,action}
    report["provenance"]     # where the archive came from, and how stale it is
"""
import json
import pathlib

from . import config


def _read_json(path: pathlib.Path):
    """Any unreadable file is 'absent', never an exception.

    pipeline/refresh.py writes two of these and is being actively developed in
    another session; preflight must tolerate a half-written or reshaped file
    rather than take the whole splash down with it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                            # noqa: BLE001
        return None


def _check(key, label, ok, detail, fix=None, action=None, command=None):
    """One row of the splash checklist.

    Three different kinds of remedy, kept apart because they are shown apart:

    `fix`      a sentence a person acts on ("ask whoever gave you this app").
    `command`  something to type in a terminal. Rendered as code, and only ever
               offered where a terminal and a checkout exist -- see _cmd().
    `action`   a step the APP can run itself (pakpatat/firstrun.py), which the
               splash draws as a button. Most gaps have no such step, and a
               button that cannot work is worse than a sentence that can.
    """
    return {"key": key, "label": label, "ok": bool(ok), "detail": detail,
            "fixable": bool(fix or command or action),
            "fix": fix, "command": command, "action": action}


def _cmd(command: str) -> str | None:
    """A terminal command, but only where there is a terminal to type it in.

    An installed build has no repository, no `pipeline/` directory and often no
    `python` on PATH, so telling its user to run `python build_index.py` names
    a file that is not on their computer. The frozen app gets a button or a
    sentence instead; a developer running from a checkout still gets the
    command, which is genuinely the fastest fix there.
    """
    return None if config.FROZEN else command


# ------------------------------------------------------------------- checks
def check_corpus() -> dict:
    """The archive itself.

    Used to be the one gap no button could close: build_corpus.py reads source
    pages the operator holds and this project does not distribute (NOTICE.md),
    so an installed copy either shipped with the archive or it did not. That is
    still true of the RETIRED site and any partner materials -- they live only
    in someone's existing copy, and no button can conjure them. But the LIVE
    site is public, and pakpatat/archive.py can crawl it directly and politely
    (robots.txt, rate limiting, a request budget -- see that module), so an
    install with nothing is no longer stuck asking a person for a copy when
    the internet already has one.
    """
    if not config.CORPUS.exists():
        if config.FROZEN:
            return _check("corpus", "Archive", False,
                          "This copy of the app was installed without the "
                          "archive. You can download the current guidance "
                          "from help.unhcr.org now, or ask whoever gave you "
                          "this app for a copy that also includes "
                          "retired-site records and partner materials.",
                          action="crawl_archive")
        if config.ARCHIVE_ROOT is None:
            return _check("corpus", "Archive", False,
                          "No corpus on this computer, and PAKPATAT_ARCHIVE "
                          "is not set to your copy of the source pages. You "
                          "can download the current guidance from "
                          "help.unhcr.org now, or point PAKPATAT_ARCHIVE at "
                          "a fuller copy.",
                          command="export PAKPATAT_ARCHIVE=~/path/to/archive",
                          action="crawl_archive")
        return _check("corpus", "Archive", False,
                      "No corpus on this computer.",
                      command=_cmd("python pipeline/build_corpus.py"),
                      action="crawl_archive")

    n = _count_corpus()
    manifest = _read_json(config.DATA_DIR / "kb_manifest.json") or {}
    built, stated = manifest.get("built"), manifest.get("total_chunks")

    detail = f"{n:,} passages" if isinstance(n, int) else "present"
    if built:
        # Only quote the manifest's date when the manifest still describes this
        # corpus. promote() does not copy kb_manifest.json, so after a refresh
        # its date belongs to the PREVIOUS build -- and a confidently wrong
        # "built <date>" under an archive of guidance is the kind of small lie
        # this project exists to not tell.
        detail += (f", built {built}" if stated == n
                   else f" (summary file is out of date, from {built})")
    return _check("corpus", "Archive", True, detail)


def _count_corpus() -> int | None:
    """Line count of corpus.jsonl -- the authoritative number.

    NOT kb_manifest.json's `total_chunks`. The manifest is a summary written by
    build_corpus.py, and pipeline/refresh.py's promote step copies corpus.jsonl
    and index/ but not the manifest -- so after a promote the manifest still
    reports the PREVIOUS build's count. Trusting it made this check accuse the
    index of being stale when the index was correct and the manifest was not.
    """
    try:
        with open(config.CORPUS, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:                                            # noqa: BLE001
        return None


def check_index() -> dict:
    """Present is not enough -- it must MATCH the corpus.

    A corpus rebuilt without re-running build_index.py leaves an index that
    still loads, still searches, and silently answers from the passages the
    archive used to hold. That is a stale hotline number with no warning
    attached, so a count mismatch is a hard failure here, not a note.
    """
    # Buildable ONLY when the corpus is here; offering "Build it now" with no
    # corpus is a button whose entire function is to produce an error -- and so
    # is `python build_index.py`, which exits on a missing corpus. With nothing
    # to index, the only true thing to say is that this row is waiting on the
    # one above it.
    act = "rebuild_index" if config.CORPUS.exists() else None
    waiting = None if act else "Waiting for the archive above."

    if not (config.INDEX_VECTORS.exists() and config.INDEX_META.exists()):
        return _check("index", "Search index", False,
                      "Not built yet.", fix=waiting,
                      command=_cmd("python build_index.py") if act else None,
                      action=act)

    meta = _read_json(config.INDEX_META)
    if meta is None:
        return _check("index", "Search index", False,
                      "Unreadable -- rebuild it.", fix=waiting,
                      command=_cmd("python build_index.py") if act else None,
                      action=act)

    n_index = len(meta) if isinstance(meta, list) else len(meta.get("chunks", []))
    n_corpus = _count_corpus()

    if isinstance(n_corpus, int) and n_index != n_corpus:
        return _check("index", "Search index", False,
                      f"Stale: {n_index:,} indexed but the archive holds "
                      f"{n_corpus:,}. Some pages cannot be found.",
                      command=_cmd("python build_index.py"), action=act)
    return _check("index", "Search index", True, f"{n_index:,} passages indexed")


def _cache_has_model(root: pathlib.Path) -> bool:
    """Is a fastembed model present under `root`?

    Looks for the `models--*` directory rather than for *.onnx: fastembed uses
    the HuggingFace blob layout, where the weights are content-addressed files
    with hash names and no extension. Globbing for *.onnx found nothing and
    reported a fully-cached model as missing.
    """
    try:
        return root.is_dir() and any(root.glob("models--*"))
    except Exception:                                            # noqa: BLE001
        return False


def check_embedder() -> dict:
    """The ONNX embedding model -- ~220MB, and needed for EVERY question.

    This is blocking. retrieve.py:149 embeds the query itself, so without this
    the app cannot search at all; it is not, as an earlier version of this file
    assumed, only needed to index new text.
    """
    if _cache_has_model(config.EMBED_CACHE):
        return _check("embedder", "Language model (search)", True,
                      "Cached — works offline")

    # Building the index is what pulls this model down and pins it beside the
    # data, so both cases below are fixed by that one step -- but only where
    # there is a corpus to index, so the button and the command come and go
    # with it.
    act = "rebuild_index" if config.CORPUS.exists() else None

    # Downloaded, but into the OS temp directory fastembed defaults to. It works
    # today and may be deleted tomorrow, which is a different problem needing
    # different advice -- so say so rather than reporting a flat OK.
    if _cache_has_model(config.LEGACY_EMBED_CACHE):
        return _check("embedder", "Language model (search)", True,
                      "Cached in a temporary system folder — the computer may "
                      "delete it. Re-run the index build once to store it "
                      "permanently.",
                      command=_cmd("python build_index.py") if act else None,
                      action=act)

    return _check("embedder", "Language model (search)", False,
                  "Not downloaded yet (~220MB, once — needs internet).",
                  # With no corpus there is nothing to trigger the download, so
                  # say what it is waiting for rather than offering a dead end.
                  fix=None if act else "Downloads with the first index build.",
                  command=_cmd("python build_index.py") if act else None,
                  action=act)


def check_answerer() -> dict:
    """Whichever provider is configured -- local engine, or a cloud key.

    Three failures hide behind one "not ready" here and they need three
    different things from the user, so they are separated rather than summed:
    Ollama absent (install a second program), Ollama present but the model not
    downloaded (a 2GB download the app can run itself), and a cloud provider
    with no key (open Settings).
    """
    from . import settings
    try:
        ready, msg = settings.check_ready()
    except Exception as e:                                       # noqa: BLE001
        return _check("answerer", "Answering model", False, str(e))

    if config.MODEL_PROVIDER != "ollama":
        return _check("answerer", f"Answering model ({config.MODEL_PROVIDER})",
                      ready, config.MODEL_NAME if ready else msg,
                      fix=None if ready else "Open Settings and paste your API key.",
                      action=None if ready else "settings")

    label = "Answering model (offline)"
    if ready:
        return _check("answerer", label, True, config.MODEL_NAME)

    from . import ollama
    if ollama.executable() is None and not ollama.is_up():
        return _check("answerer", label, False,
                      "The local AI engine (Ollama) is not installed on this "
                      "computer.",
                      fix="Install Ollama, then reopen this app — or use "
                          "Settings to answer with an online provider instead.",
                      action="get_ollama")

    if ollama.is_up() and not ollama.has_model(config.MODEL_NAME):
        return _check("answerer", label, False,
                      f"{config.MODEL_NAME} has not been downloaded yet "
                      f"(~2GB, once — needs internet).",
                      command=_cmd(f"ollama pull {config.MODEL_NAME}"),
                      action="pull_model")

    # Installed, not answering yet -- ollama.nudge() has already asked it to
    # start. No action: the only useful thing anyone can do is wait a moment.
    return _check("answerer", label, False, msg)


def check_privacy() -> dict:
    """States plainly whether questions leave this computer.

    Shown because it is the single fact a case worker is most often asked by
    the person sitting next to them, and the honest answer depends on a setting
    they may not have chosen themselves.
    """
    if config.MODEL_PROVIDER == "ollama":
        return _check("privacy", "Privacy", True,
                      "Questions stay on this computer.")
    return _check("privacy", "Privacy", True,
                  f"Questions are sent to {config.MODEL_PROVIDER}.")


# --------------------------------------------------------------- provenance
def provenance() -> dict:
    """Where the archive came from and how stale it is.

    Read-only over the files pipeline/refresh.py maintains. It never triggers a
    refresh: fetching is the operator's decision and publishing a refresh is a
    human one (see that module's docstring), so the app only ever REPORTS.
    """
    manifest = _read_json(config.DATA_DIR / "kb_manifest.json") or {}
    state = _read_json(config.DATA_DIR / "refresh_state.json") or {}

    history = state.get("history") if isinstance(state.get("history"), list) else []
    last = history[-1] if history else {}
    pending = state.get("pending") or {}
    n_pending = len(pending) if hasattr(pending, "__len__") else 0

    return {
        "built": manifest.get("built"),
        "captured": state.get("captured"),
        "last_promoted": last.get("promoted") if isinstance(last, dict) else None,
        "pending_count": n_pending,
        "sources": manifest.get("sources") or {},
        "credit": "Guidance published by UNHCR and its partners.",
        "not_affiliated": (
            "This is an independent tool. It is not affiliated with, endorsed "
            "by, or operated by UNHCR."
        ),
    }


# --------------------------------------------------------------------- run
def run() -> dict:
    """All checks. `ready` covers exactly what is needed to answer a question.

    The embedder is blocking: retrieve.py embeds the query, so a missing cache
    means no search at all, not merely no re-indexing. Privacy is reported but
    never blocks -- it states a fact about where questions go, and both answers
    are legitimate.
    """
    checks = [check_corpus(), check_index(), check_embedder(),
              check_answerer(), check_privacy()]
    blocking = {"corpus", "index", "embedder", "answerer"}
    ready = all(c["ok"] for c in checks if c["key"] in blocking)
    return {"ready": ready, "checks": checks, "provenance": provenance()}


if __name__ == "__main__":
    from . import brand
    r = run()
    print(brand.console_banner())
    for c in r["checks"]:
        print(f"  {'OK ' if c['ok'] else 'XX '} {c['label']:<32} {c['detail']}")
        if not c["ok"] and (c["command"] or c["fix"]):
            print(f"       fix: {c['command'] or c['fix']}")
    print(f"\n  {'Ready.' if r['ready'] else 'Not ready.'}")
