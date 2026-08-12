#!/usr/bin/env python3
"""
Päkpätät -- desktop app.

Runs a small local server (127.0.0.1 only, never exposed to the network) and
opens it in a NATIVE desktop window via pywebview, which uses the operating
system's own browser engine:
    macOS   -> WKWebView   (built in)
    Windows -> Edge WebView2 (built into Windows 10/11)
    Linux   -> WebKitGTK
So there is no Electron, no Node.js, and nothing extra for a non-technical
user to install.

If the native window can't start for any reason, it falls back to opening the
default web browser -- the app still works either way.

Run:  python app.py
"""
import http.server
import json
import os
import pathlib
import socket
import socketserver
import sys
import threading
import webbrowser

# Resources come from the BUNDLE root, which is the repository when running from
# a checkout and sys._MEIPASS inside a PyInstaller build. Resolving them from
# __file__ worked in development and produced a frozen app that opened a window
# and served a 500 for its own UI.
from pakpatat import config as _config          # noqa: E402  (needed for paths)

HERE = _config.BUNDLE
UI_FILE = HERE / "ui" / "index.html"


def _clean_history(raw) -> list[dict]:
    """Normalise the conversation the UI sends alongside a follow-up question.

    The browser is a client like any other, so its input is bounded here rather
    than trusted: last few turns only, known roles only, each turn truncated.
    An oversized history would otherwise push the archive sources out of the
    model's context window -- which is exactly how a phone number goes missing.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw[-6:]:
        if not isinstance(m, dict):
            continue
        role = "user" if m.get("role") == "user" else "bot"
        text = m.get("text")
        if isinstance(text, str) and text.strip():
            out.append({"role": role, "text": text.strip()[:1000]})
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# A local 7B model on a laptop can take a minute or more to read 8 archive
# chunks and write an answer. The OS webview (WKWebView / WebView2) gives up on
# a request that sends NOTHING for ~60 seconds, and the UI could only report
# that as "could not reach the service" -- a flat lie, since the answer was
# still being written and arrived fine seconds later.
#
# So /ask streams: it sends a progress line immediately and another every few
# seconds while the model works. Bytes keep flowing, the idle timer never
# fires, and the person asking sees a running count instead of silence.
HEARTBEAT_SECONDS = 3.0


class Handler(http.server.BaseHTTPRequestHandler):
    # Chunked streaming needs HTTP/1.1. Every other response sets an accurate
    # Content-Length in _send, so keep-alive is safe.
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _open_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _chunk(self, obj) -> bool:
        """Write one NDJSON line as an HTTP chunk. False if the client hung up
        (they closed the window mid-answer -- not an error worth reporting)."""
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _close_stream(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/settings":
            from pakpatat import settings
            state = settings.current()
            state["ready"], state["ready_message"] = settings.check_ready()
            self._json(state)
        elif self.path == "/health":
            self._json({"ok": True})
        elif self.path == "/preflight":
            # What the splash waits on. Everything here is local and read-only,
            # so it answers in milliseconds and never blocks on the network.
            from pakpatat import preflight
            self._json(preflight.run())
        elif self.path == "/brand":
            from pakpatat import brand
            self._json({
                "name": brand.NAME, "gloss": brand.GLOSS_LONG,
                "tagline": brand.TAGLINE, "colors": brand.COLORS,
                "mark_path": brand.MARK_PATH,
                "credit": brand.SOURCE_CREDIT,
                "not_affiliated": brand.NOT_AFFILIATED,
                "takedown": brand.TAKEDOWN,
            })
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/settings":
            from pakpatat import settings
            try:
                state = settings.save(
                    provider=payload.get("provider", ""),
                    model=payload.get("model"),
                    api_key=payload.get("api_key"),
                )
                state["ready"], state["ready_message"] = settings.check_ready()
                self._json(state)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 400)
            return

        if self.path == "/post":
            # Render an answer as a shareable image. The UI sends back the whole
            # message object it already holds -- answer, warnings, unverified
            # facts and sources -- because the picture must carry all of them,
            # not just the prose. See pakpatat/postcard.py.
            from pakpatat import postcard
            try:
                paths = postcard.render(
                    payload.get("message") or {},
                    payload.get("question") or "",
                    theme=payload.get("theme") or "light",
                    lang=payload.get("lang") or "en",
                    out_path=payload.get("dir") or None,
                )
                self._json({"ok": True, "count": len(paths),
                            "folder": str(paths[0].parent),
                            "paths": [str(p) for p in paths]})
            except postcard.TooLong:
                self._json({"error": "too_long"}, 413)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)
            return

        if self.path == "/reveal":
            # Open the folder the images landed in. "Saved to Downloads" is only
            # useful to someone who knows where Downloads is; showing them the
            # folder removes the step where they go looking and give up.
            import subprocess
            import sys as _sys
            folder = pathlib.Path(payload.get("folder") or "").expanduser()
            try:
                if folder.is_dir():
                    if _sys.platform == "darwin":
                        subprocess.Popen(["open", str(folder)])
                    elif _sys.platform == "win32":
                        subprocess.Popen(["explorer", str(folder)])
                    else:
                        subprocess.Popen(["xdg-open", str(folder)])
                self._json({"ok": True})
            except OSError as e:
                self._json({"error": str(e)}, 500)
            return

        if self.path != "/ask":
            self._send(404, b"not found", "text/plain")
            return

        question = (payload.get("question") or "").strip()
        source_filter = payload.get("source_filter") or None
        history = _clean_history(payload.get("history"))
        if not question:
            self._json({"error": "empty question"}, 400)
            return

        self._open_stream()
        if not self._chunk({"stage": "searching", "elapsed": 0}):
            return

        # Check the provider is usable BEFORE spending time retrieving, so the
        # user gets "Ollama isn't running" rather than a stack trace.
        from pakpatat import config, settings
        ready, msg = settings.check_ready()
        if not ready and config.MODEL_PROVIDER == "ollama":
            # First question after a cold start: wait for the local engine to
            # finish booting instead of making the user ask again.
            self._chunk({"stage": "starting", "elapsed": 0})
            from pakpatat import ollama
            started, _ = ollama.ensure(timeout=45.0)
            if started:
                ready, msg = settings.check_ready()
        if not ready:
            self._chunk({"done": True, "answer": msg, "sources": [],
                         "warnings": [], "refused": True, "top_score": 0.0,
                         "setup_error": True})
            self._close_stream()
            return

        # Run the graph on a worker thread so this one stays free to keep the
        # connection alive with progress lines.
        box: dict = {}

        def work() -> None:
            try:
                from pakpatat import graph
                box["result"] = graph.ask(question, source_filter=source_filter,
                                          history=history)
            except SystemExit as e:        # missing key / missing index
                box["result"] = {"answer": str(e), "sources": [], "warnings": [],
                                 "refused": True, "top_score": 0.0,
                                 "setup_error": True}
            except Exception as e:         # noqa: BLE001 - surface to the UI
                box["result"] = {"answer": f"Something went wrong: {e}",
                                 "sources": [],
                                 "warnings": ["The assistant hit an unexpected error."],
                                 "refused": True, "top_score": 0.0,
                                 "setup_error": True}

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        elapsed = 0.0
        while worker.is_alive():
            worker.join(timeout=HEARTBEAT_SECONDS)
            if not worker.is_alive():
                break
            elapsed += HEARTBEAT_SECONDS
            # Retrieval is fast; anything past a couple of seconds is the model
            # writing. Say so, rather than leaving "searching" up for a minute.
            stage = "searching" if elapsed < HEARTBEAT_SECONDS * 2 else "writing"
            if not self._chunk({"stage": stage, "elapsed": int(elapsed)}):
                return  # window closed; the worker finishes and exits on its own

        self._chunk({"done": True, **box["result"]})
        self._close_stream()

    def log_message(self, *args):  # silence per-request console spam
        pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def set_app_icon() -> None:
    """Put the owl in the Dock / taskbar instead of the Python rocket.

    A page favicon does NOT reach the window chrome: WKWebView and WebView2 both
    render the page and leave the app icon to the host process, which for
    `python app.py` is the interpreter. So the app was fully branded everywhere
    except the one place a user looks to find it again after switching windows.

    macOS: pyobjc ships with pywebview here, so the Dock icon can be set on the
    running process. This is the honest fix short of building a .app bundle --
    it lasts for the session and needs no packaging step.

    Windows: the taskbar icon comes from the running .exe, so it stays Python's
    until the app is frozen (PyInstaller etc.) with ui/brand/icon.ico. Setting
    an AppUserModelID here would only change grouping, not the icon, so this
    deliberately does nothing rather than pretend.
    """
    icns = HERE / "ui" / "brand" / "icon.icns"
    png = HERE / "ui" / "brand" / "icons" / "icon-512.png"
    src = icns if icns.exists() else png
    if not src.exists():
        return
    try:
        from AppKit import NSApplication, NSImage           # macOS only
        image = NSImage.alloc().initWithContentsOfFile_(str(src))
        if image:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:                                        # noqa: BLE001
        pass          # every other platform, and any pyobjc surprise: not fatal


def main() -> None:
    # If this computer answers questions locally, bring Ollama up while the
    # window is still opening -- by the time a question is typed it's ready.
    from pakpatat import config, ollama
    if config.MODEL_PROVIDER == "ollama":
        ollama.start_in_background()

    from pakpatat import brand

    port = free_port()
    url = f"http://127.0.0.1:{port}/"

    server = Server(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(brand.console_banner())
    print(f"  running at {url}")

    try:
        import webview  # native OS window

        class _Api:
            """The only bridge from the page into Python.

            It exists for one thing: a real "choose a folder" dialog when
            saving an image. A local web page cannot open one, and a webview
            cannot do a normal browser download either -- so without this the
            only options were a fixed folder or nothing. Deliberately one
            method, returning a path and touching nothing else.
            """

            def choose_folder(self):
                try:
                    win = webview.windows[0]
                    dialog = getattr(webview, "FileDialog", None)
                    kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
                    picked = win.create_file_dialog(kind)
                    return picked[0] if picked else None
                except Exception:                            # noqa: BLE001
                    return None                              # fall back to Downloads

        webview.create_window(
            brand.NAME,
            url,
            js_api=_Api(),
            width=1000, height=780, min_size=(640, 560),
            # The window paints before the page does. Without this it flashes
            # white on the way to a dark-mode splash, which is exactly the kind
            # of cheap-feeling detail a case worker reads as "unfinished".
            background_color=brand.COLORS["light"]["paper"],
        )
        # pywebview's own icon= is GTK/Qt only -- it covers Linux and is
        # ignored elsewhere. macOS is handled by set_app_icon() above.
        icon = HERE / "ui" / "brand" / "icons" / "icon-512.png"
        try:
            webview.start(icon=str(icon) if icon.exists() else None)
        except TypeError:
            webview.start()          # pywebview too old to accept the argument
    except Exception as e:  # noqa: BLE001
        print(f"(Native window unavailable: {e})")
        print("Opening in your web browser instead. Close this window to quit.")
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


def selftest() -> int:
    """Prove a frozen build can find its own parts. Exits non-zero if not.

    The way a PyInstaller build of this app fails is not a crash on launch: it
    opens a window and then serves a 500 for its own UI, or raises on the first
    question because a provider module was never collected. Both look like
    application bugs and neither shows up in a checkout, so the build pipeline
    checks for them explicitly.
    """
    from pakpatat import brand, config
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'OK ' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")

    print(brand.console_banner(ascii_only=True))
    print(f"  version {__import__('pakpatat').__version__}  frozen={config.FROZEN}")
    print(f"  bundle  {config.BUNDLE}")
    print(f"  home    {config.HOME}")
    check("ui/index.html present", UI_FILE.exists(), str(UI_FILE))
    check("brand icons present", (config.BUNDLE / "ui" / "brand").is_dir())
    check("writable state dir", os.access(config.HOME, os.W_OK), str(config.HOME))
    for mod in ("pakpatat.graph", "pakpatat.retrieve", "pakpatat.postcard",
                "pakpatat.preflight", "pakpatat.settings"):
        try:
            __import__(mod); check(f"import {mod}", True)
        except Exception as e:                                # noqa: BLE001
            check(f"import {mod}", False, repr(e))
    try:
        import onnxruntime                                    # noqa: F401
        check("onnxruntime loads", True)
    except Exception as e:                                    # noqa: BLE001
        check("onnxruntime loads", False, repr(e))
    print(f"\n  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
