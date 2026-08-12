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

Everything here is read-only and offline. Nothing in this module fetches.

    from pakpatat import preflight
    report = preflight.run()
    report["ready"]          # bool -- can it answer right now?
    report["checks"]         # list of {key, label, ok, detail, fixable, fix}
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


def _check(key, label, ok, detail, fix=None):
    return {"key": key, "label": label, "ok": bool(ok), "detail": detail,
            "fixable": fix is not None, "fix": fix}


# ------------------------------------------------------------------- checks
def check_corpus() -> dict:
    if not config.CORPUS.exists():
        return _check("corpus", "Archive", False,
                      "No corpus on this computer.",
                      "python pipeline/build_corpus.py")

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
    if not (config.INDEX_VECTORS.exists() and config.INDEX_META.exists()):
        return _check("index", "Search index", False,
                      "Not built yet.", "python build_index.py")

    meta = _read_json(config.INDEX_META)
    if meta is None:
        return _check("index", "Search index", False,
                      "Unreadable -- rebuild it.", "python build_index.py")

    n_index = len(meta) if isinstance(meta, list) else len(meta.get("chunks", []))
    n_corpus = _count_corpus()

    if isinstance(n_corpus, int) and n_index != n_corpus:
        return _check("index", "Search index", False,
                      f"Stale: {n_index:,} indexed but the archive holds "
                      f"{n_corpus:,}. Some pages cannot be found.",
                      "python build_index.py")
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

    # Downloaded, but into the OS temp directory fastembed defaults to. It works
    # today and may be deleted tomorrow, which is a different problem needing
    # different advice -- so say so rather than reporting a flat OK.
    if _cache_has_model(config.LEGACY_EMBED_CACHE):
        return _check("embedder", "Language model (search)", True,
                      "Cached in a temporary system folder — the computer may "
                      "delete it. Re-run the index build once to store it "
                      "permanently.",
                      "python build_index.py")

    return _check("embedder", "Language model (search)", False,
                  "Not downloaded yet (~220MB, once — needs internet).",
                  "python build_index.py")


def check_answerer() -> dict:
    """Whichever provider is configured -- local engine, or a cloud key."""
    from . import settings
    try:
        ready, msg = settings.check_ready()
    except Exception as e:                                       # noqa: BLE001
        return _check("answerer", "Answering model", False, str(e))

    if config.MODEL_PROVIDER == "ollama":
        label, fix = "Answering model (offline)", f"ollama pull {config.MODEL_NAME}"
    else:
        label, fix = f"Answering model ({config.MODEL_PROVIDER})", None
    return _check("answerer", label, ready,
                  config.MODEL_NAME if ready else msg, None if ready else fix)


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
        if not c["ok"] and c["fix"]:
            print(f"       fix: {c['fix']}")
    print(f"\n  {'Ready.' if r['ready'] else 'Not ready.'}")
