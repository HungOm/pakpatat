#!/usr/bin/env python3
"""
Build a single, chunked, metadata-tagged corpus across BOTH archives --
the old refugeemalaysia.org capture (01_support_topics/) and the new
help.unhcr.org/malaysia capture (04_help_unhcr_2026/) -- plus any partner
materials the operator was handed directly (07_partner_materials/).

THE CHUNKING ITSELF NOW LIVES IN pakpatat/corpus.py. It moved there unchanged
so the desktop app can rebuild the corpus in-process after refreshing the
archive from a button; an installed build has no `pipeline/` directory and no
`python` on PATH, so as a script-only step it was unreachable to every user who
was not a developer at a terminal. This file is the command-line front door to
the same code: it resolves the environment, calls corpus.build(), and prints.

This reads an archive that the OPERATOR already holds locally. No source
material ships with this repository (see NOTICE.md) -- point PAKPATAT_ARCHIVE
at your own copy:

    export PAKPATAT_ARCHIVE=~/path/to/archive
    python pipeline/build_corpus.py

Expected layout under PAKPATAT_ARCHIVE (override any of them by env var):
  01_support_topics/            retired-site capture, one dir per page
  04_help_unhcr_2026/           live-site capture + _index.json
  05_intelligence/gap_analysis/gap_analysis.json   (optional)
  07_partner_materials/         operator-supplied material            (optional)

Output (into PAKPATAT_DATA, default ./data):
  corpus.jsonl     one JSON object per chunk (the embeddable corpus)
  kb_manifest.json summary: doc count, chunk count, per-source breakdown
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pakpatat import corpus                                    # noqa: E402

_env = os.getenv("PAKPATAT_ARCHIVE")
if not _env:
    sys.exit(
        "PAKPATAT_ARCHIVE is not set.\n"
        "Point it at your local copy of the source archive, e.g.\n"
        "    export PAKPATAT_ARCHIVE=~/Desktop/refugee_malaysia\n"
        "This repository ships no archive content of its own -- see NOTICE.md."
    )

ROOT = pathlib.Path(_env).expanduser().resolve()
OLD_DIR = pathlib.Path(os.getenv("PAKPATAT_OLD_DIR", ROOT / corpus.OLD_SUBDIR))
NEW_DIR = pathlib.Path(os.getenv("PAKPATAT_NEW_DIR", ROOT / corpus.NEW_SUBDIR))
GAP = pathlib.Path(os.getenv("PAKPATAT_GAP", ROOT / corpus.GAP_SUBPATH))
PARTNER_DIR = pathlib.Path(os.getenv("PAKPATAT_PARTNER", ROOT / corpus.PARTNER_SUBDIR))
OUT = pathlib.Path(os.getenv("PAKPATAT_DATA",
                             pathlib.Path(__file__).resolve().parents[1] / "data"))


def build():
    corpus.build(root=ROOT, out_dir=OUT, old_dir=OLD_DIR, new_dir=NEW_DIR,
                 partner_dir=PARTNER_DIR, gap_path=GAP)


if __name__ == "__main__":
    build()
