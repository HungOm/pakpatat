# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Windows build.

    pyinstaller packaging/pakpatat.spec --noconfirm

ONE-FOLDER, NOT ONE-FILE. A one-file exe unpacks ~500MB to a temp directory on
every launch, which on a modest laptop is several seconds of nothing happening
before the splash appears -- and this app is used by people who will reasonably
conclude it is broken. The folder is hidden inside the install directory and the
user only ever sees the Start Menu shortcut, so one-file buys nothing here.

WHAT GOES IN, AND WHAT DELIBERATELY DOES NOT
--------------------------------------------
IN   ui/ (the interface), ui/brand/ (mark, icons), the package itself.
IN   the embedding model, IF it is already cached on the build machine -- see
     the block below. This is what makes a fresh install able to search offline.
OUT  data/ -- the archive. It is (c) UNHCR and NOTICE.md commits this project
     to not redistributing it. Opt in with PAKPATAT_BUNDLE_ARCHIVE=1 only if
     you have the right to hand that content to whoever gets the installer.
OUT  mcp_server.py and the refresh pipeline. They are operator tools, not part
     of a case worker's desktop app, and each drags in dependencies.
"""
import os
import pathlib
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = pathlib.Path(SPECPATH).resolve().parents[0].parent
sys.path.insert(0, str(ROOT))

datas = [
    (str(ROOT / "ui" / "index.html"), "ui"),
    (str(ROOT / "ui" / "brand"), "ui/brand"),
    (str(ROOT / "NOTICE.md"), "."),
    (str(ROOT / "LICENSE"), "."),
]
binaries = []
hiddenimports = [
    "pakpatat.graph", "pakpatat.retrieve", "pakpatat.factcheck",
    "pakpatat.settings", "pakpatat.ollama", "pakpatat.preflight",
    "pakpatat.postcard", "pakpatat.brand", "pakpatat.index",
]

# onnxruntime, tokenizers and fastembed all ship native libraries and load
# things by name at runtime, so PyInstaller's static analysis misses them.
for pkg in ("onnxruntime", "fastembed", "tokenizers"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as exc:                                  # noqa: BLE001
        print(f"[spec] WARNING: could not collect {pkg}: {exc}")

# langchain resolves providers through entry points; without the metadata the
# Settings panel's provider switch raises at runtime instead of at build.
for pkg in ("langchain", "langchain_core", "langchain_ollama", "langchain_community"):
    try:
        datas += collect_data_files(pkg, include_py_files=False)
        hiddenimports.append(pkg)
    except Exception:                                         # noqa: BLE001
        pass

# The ~220MB ONNX embedding model. Bundling it is the difference between an
# installer that searches offline immediately and one whose first question
# needs internet -- on a machine that may not have any.
#
# Bundled only if the build machine already has it (config.EMBED_CACHE), so a
# CI runner without the cache produces a smaller installer rather than failing.
try:
    from pakpatat import config as _cfg
    cache = _cfg.EMBED_CACHE
    if cache.exists() and any(cache.glob("models--*")):
        datas.append((str(cache), "models"))
        print(f"[spec] bundling embedding model from {cache}")
    else:
        print("[spec] NOTE: no embedding model cached; the installed app will "
              "download ~220MB on first use. Run build_index.py once before "
              "building to bundle it.")
except Exception as exc:                                      # noqa: BLE001
    print(f"[spec] WARNING: embedding-model check failed: {exc}")

# The archive. OFF unless explicitly requested -- see NOTICE.md.
if os.getenv("PAKPATAT_BUNDLE_ARCHIVE") == "1":
    data_dir = ROOT / "data"
    if data_dir.exists():
        for name in ("corpus.jsonl", "kb_manifest.json"):
            if (data_dir / name).exists():
                datas.append((str(data_dir / name), "archive"))
        if (data_dir / "index").exists():
            datas.append((str(data_dir / "index"), "archive/index"))
        print("[spec] *** BUNDLING ARCHIVE CONTENT -- confirm you have the "
              "right to distribute it (NOTICE.md) ***")

version_file = ROOT / "packaging" / "version_info.txt"
icon_file = ROOT / "ui" / "brand" / "icon.ico"

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Providers the desktop app does not use by default. Removing them keeps
    # the installer materially smaller; the Settings panel still offers Ollama,
    # which is the default and the only one that works with no account.
    excludes=["mcp", "tkinter", "matplotlib", "pytest", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Pakpatat",
    debug=False,
    strip=False,
    upx=False,              # UPX-packed exes are a routine antivirus false positive
    console=False,          # GUI app: no console window behind the desktop window
    icon=str(icon_file) if icon_file.exists() else None,
    version=str(version_file) if version_file.exists() else None,
)

coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="Pakpatat",
)
