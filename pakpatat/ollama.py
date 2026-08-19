"""
Start the local Ollama server on demand.

A non-technical user should not have to know that a second program has to be
running before they can ask a question. When the app launches with the local
provider selected, we look for Ollama on this computer and start it in the
background. If it isn't installed we say so plainly instead of failing later
with a connection error.

Nothing here downloads models -- that can be several gigabytes, so it stays a
deliberate choice by the user (pakpatat/firstrun.py does it from a button, with
a progress bar, once the user asks for it).

It DOES load an already-downloaded model into memory at launch -- see warm()
below for why that is the difference between a 7-second answer and a
four-minute one.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:11434"

# Launching from Finder / the Start Menu gives a minimal PATH, so `which` alone
# is not enough -- check the places the official installers actually use.
_CANDIDATES = {
    "darwin": [
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
        "~/Applications/Ollama.app/Contents/Resources/ollama",
    ],
    "win32": [
        "~/AppData/Local/Programs/Ollama/ollama.exe",
        "C:/Program Files/Ollama/ollama.exe",
    ],
    "linux": [
        "/usr/local/bin/ollama",
        "/usr/bin/ollama",
        "~/.local/bin/ollama",
    ],
}

_lock = threading.Lock()
_launch_attempted_at = 0.0   # rate-limits repeat launch attempts
_launch_succeeded = False    # ...and remembers whether that attempt worked
_RETRY_AFTER = 20.0          # seconds


def host() -> str:
    # OLLAMA_HOST is commonly set without a scheme ("127.0.0.1:11434"), which
    # urllib will not accept -- normalise it.
    h = (os.getenv("OLLAMA_HOST") or DEFAULT_HOST).strip().rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h


def _get(path: str, timeout: float):
    with urllib.request.urlopen(f"{host()}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def is_up(timeout: float = 1.5) -> bool:
    """Is the Ollama server answering right now?"""
    try:
        _get("/api/tags", timeout)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def installed_models(timeout: float = 2.0) -> list[str]:
    try:
        return [m["name"] for m in _get("/api/tags", timeout).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return []


def has_model(name: str, timeout: float = 2.0) -> bool:
    """True if `name` is pulled. Treats 'llama3' and 'llama3:latest' as equal."""
    want = name if ":" in name else f"{name}:latest"
    return any(m == want or m.split(":")[0] == name for m in installed_models(timeout))


def executable() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    for raw in _CANDIDATES.get(sys.platform, _CANDIDATES["linux"]):
        p = pathlib.Path(raw).expanduser()
        if p.exists():
            return str(p)
    return None


def _spawn() -> bool:
    """Launch the server in the background. True if a launch was attempted."""
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        # Don't flash a console window in the user's face.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True   # survives this app quitting

    exe = executable()
    if exe:
        try:
            subprocess.Popen([exe, "serve"], **kwargs)
            return True
        except OSError:
            pass

    if sys.platform == "darwin":
        # Fall back to the menu-bar app, which starts the same server. `open`
        # itself always launches, so only claim success if the app is really
        # there -- otherwise we'd report "starting…" forever.
        app = next((p for p in (pathlib.Path("/Applications/Ollama.app"),
                                pathlib.Path("~/Applications/Ollama.app").expanduser())
                    if p.exists()), None)
        if app:
            try:
                subprocess.Popen(["open", "-ga", str(app)], **kwargs)
                return True
            except OSError:
                pass
    return False


def nudge() -> bool:
    """
    Ask for a start without blocking. Safe to call often -- it does nothing if
    the server is already up or a launch was attempted moments ago.
    """
    global _launch_attempted_at, _launch_succeeded
    with _lock:
        if is_up(timeout=0.8):
            return True
        if time.monotonic() - _launch_attempted_at < _RETRY_AFTER:
            # Reuse the last verdict: a launch that failed because Ollama isn't
            # installed must not keep reporting "starting…".
            return _launch_succeeded
        _launch_attempted_at = time.monotonic()
        _launch_succeeded = _spawn()
        return _launch_succeeded


def ensure(timeout: float = 30.0) -> tuple[bool, str]:
    """
    Make sure Ollama is running, waiting up to `timeout` seconds for it to come
    up. Returns (ok, message-for-the-user).
    """
    if is_up():
        return True, ""
    if not _spawn():
        return False, ("Ollama is not installed on this computer. Install it "
                       "from ollama.com/download, then reopen this app -- or "
                       "click Settings to use an online provider instead.")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.7)
        if is_up():
            return True, ""
    return False, ("Ollama was started but is not responding yet. Give it a "
                   "few more seconds and ask again.")


def start_in_background() -> None:
    """Fire-and-forget startup, so opening the app stays instant."""
    threading.Thread(target=ensure, kwargs={"timeout": 60.0}, daemon=True).start()


# ------------------------------------------------------- loading the weights
#
# Starting the SERVER is not the slow part. Loading the MODEL is, and until
# this existed the app paid that cost on the user's first question:
#
#     cold   load 253.0s + prefill 8.0s + generate 2.4s = 263s
#     warm   load   0.2s + prefill 0.2s + generate 6.8s =   7.3s
#
# (measured on the reference M1 under memory pressure, 2.3GB of weights at
# roughly disk speed). Four minutes of "Writing the answer…" is indistinguishable
# from a hung app, and a case worker with someone sitting across from them will
# have given up long before it lands.
#
# So the weights are pulled into memory while the splash is still on screen and
# the question is still being typed. Nothing waits on it: a question asked
# mid-load simply joins the same load already in progress.
_warm_state = "cold"      # cold | loading | ready | failed
_warm_detail = ""


def warm_state() -> tuple[str, str]:
    """(state, detail) for the UI -- so a long first load can say what it is."""
    return _warm_state, _warm_detail


def warm(model: str, num_ctx: int | None = None,
         keep_alive: str | None = None) -> bool:
    """Load `model` into memory now, generating nothing.

    An empty prompt makes Ollama load the weights and return -- the documented
    preload. The options MUST match what the app will send for real questions:
    Ollama reloads the whole model when num_ctx changes, so warming with the
    default 4096 and then asking with 8192 would pay the load cost TWICE and
    leave this function doing harm instead of nothing.
    """
    global _warm_state, _warm_detail
    payload: dict = {"model": model, "prompt": ""}
    if keep_alive:
        payload["keep_alive"] = keep_alive
    if num_ctx:
        payload["options"] = {"num_ctx": num_ctx}

    req = urllib.request.Request(
        f"{host()}/api/generate", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        # No timeout. On a machine short on RAM this legitimately takes
        # minutes, and abandoning it halfway would leave the first question to
        # start the load all over again.
        with urllib.request.urlopen(req) as r:
            r.read()
        _warm_state, _warm_detail = "ready", ""
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        _warm_state, _warm_detail = "failed", str(e)
        return False


def warm_in_background(model: str, num_ctx: int | None = None,
                       keep_alive: str | None = None) -> None:
    """Start the server if needed, then load the weights. Returns at once."""
    global _warm_state
    if _warm_state in ("loading", "ready"):
        return
    _warm_state = "loading"

    def run() -> None:
        global _warm_state, _warm_detail
        ok, msg = ensure(timeout=60.0)
        if not ok:
            _warm_state, _warm_detail = "failed", msg
            return
        if has_model(model):
            warm(model, num_ctx, keep_alive)
        else:
            # Nothing to warm -- preflight already offers to download it, and
            # pretending to load a model that is not here would report "ready"
            # for weights that do not exist.
            _warm_state, _warm_detail = "failed", "model not downloaded"

    threading.Thread(target=run, daemon=True).start()
