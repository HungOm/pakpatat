#!/usr/bin/env python3
"""
Build the local search index. Run once after install, and again any time the
archive is re-scraped and the corpus (data/corpus.jsonl) changes.

    python build_index.py
"""
from pakpatat.index import build

if __name__ == "__main__":
    build()
