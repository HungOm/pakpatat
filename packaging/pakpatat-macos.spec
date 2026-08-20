# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
PyInstaller spec for the macOS build -> Pakpatat.app

    pyinstaller packaging/pakpatat-macos.spec --noconfirm

Shares every decision with the Windows spec (see pakpatat.spec for the
reasoning on one-folder, the bundled model, and why the archive is excluded).
Only the packaging differs: macOS wants a .app bundle with an Info.plist and an
.icns, and the result is shipped as a .dmg rather than an installer.

ARCHITECTURE. PyInstaller builds for the architecture of the Python running it.
An arm64 Python produces an Apple-Silicon-only .app; Intel Macs need a build
from an x86_64 Python (or Rosetta). `target_arch='universal2'` only works when
every wheel in the tree is universal2, and onnxruntime's is not -- so this
builds for the host arch and the requirements table says which that was.
"""
import os
import pathlib
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files

# SPECPATH is the packaging/ directory itself, so one .parent reaches
# the repository root. `.parents[0].parent` climbed one level too far
# and put ROOT outside the project entirely.
ROOT = pathlib.Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))

from pakpatat import __version__ as VERSION      # noqa: E402
from pakpatat import brand                       # noqa: E402

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
    # Imported inside a request handler, so nothing static points at it and
    # PyInstaller cannot see it -- without this the frozen app's setup buttons
    # fail with ModuleNotFoundError the first time anyone presses one.
    "pakpatat.firstrun",
    # Same reason -- the Knowledge base panel's crawl/check/update
    # actions and the corpus chunker they call are both imported lazily.
    "pakpatat.archive", "pakpatat.corpus", "pakpatat.bundle",
    # bs4/markdownify: also imported lazily (inside archive.py's
    # to_markdown()), and needed by any install now that the
    # Knowledge base panel can crawl for itself.
    "bs4", "soupsieve", "markdownify",
]

for pkg in ("onnxruntime", "fastembed", "tokenizers"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as exc:                                  # noqa: BLE001
        print(f"[spec] WARNING: could not collect {pkg}: {exc}")

# NOT langchain_community: it is not in requirements.txt, and listing it as a
# hidden import made every build log an "ERROR: Hidden import not found".
for pkg in ("langchain", "langchain_core", "langchain_ollama"):
    try:
        datas += collect_data_files(pkg, include_py_files=False)
        hiddenimports.append(pkg)
    except Exception:                                         # noqa: BLE001
        pass

try:
    from pakpatat import config as _cfg
    cache = _cfg.EMBED_CACHE
    if cache.exists() and any(cache.glob("models--*")):
        datas.append((str(cache), "models"))
        print(f"[spec] bundling embedding model from {cache}")
    else:
        print("[spec] NOTE: no embedding model cached; first use will download "
              "~220MB. Run build_index.py once before building to bundle it.")
except Exception as exc:                                      # noqa: BLE001
    print(f"[spec] WARNING: embedding-model check failed: {exc}")

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

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["mcp", "tkinter", "matplotlib", "pytest", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Pakpatat",
    debug=False, strip=False, upx=False,
    console=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Pakpatat")

app = BUNDLE(
    coll,
    name="Pakpatat.app",
    icon=str(ROOT / "ui" / "brand" / "icon.icns"),
    bundle_identifier="org.pakpatat.app",
    version=VERSION,
    info_plist={
        "CFBundleName": brand.NAME_ASCII,
        "CFBundleDisplayName": brand.NAME,          # Finder shows the diaeresis
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHumanReadableCopyright":
            "MIT licensed. Ships no archive content - see NOTICE.md. "
            "Not affiliated with UNHCR.",
        # Retina: without this the webview renders at 1x and the whole UI is
        # soft on every Mac made in the last decade.
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,    # allow dark mode
        "LSMinimumSystemVersion": "11.0",
        # No camera, mic, location, contacts or network-server usage to declare:
        # the app listens on 127.0.0.1 only and reaches the network solely if
        # the operator switches to a cloud provider in Settings.
        "LSApplicationCategoryType": "public.app-category.productivity",
    },
)
