# Notice on content, sources and scope

Read this before adding data to a fork, and before publishing anything built
with Päkpätät.

## This repository contains no archive content

Päkpätät is **code only**. It ships:

- the retrieval and answering pipeline
- the corpus builder (`pipeline/build_corpus.py`)
- the evaluation harness (`eval/`)
- the desktop app and MCP server

It does **not** ship, and must not be used to redistribute:

- UNHCR web pages, PDFs, training decks, posters or photographs
- any scraped copy of `help.unhcr.org` or the retired `refugeemalaysia.org`

The source material those pages contain is **© UNHCR** (or its partners). It is
published for the benefit of refugees and asylum-seekers, but publishing it is
UNHCR's decision to make, not ours. The archive stays on the operator's own
machine, and `data/` is git-ignored for exactly this reason.

If you want to distribute the content itself, **ask UNHCR first.** Ask narrowly
(the text of the public help pages, for offline use by community
organisations), offer attribution, canonical links back, and a standing
takedown commitment.

## This project is not affiliated with UNHCR

Päkpätät is an independent tool built by a refugee community-based
organisation. It is **not** endorsed by, affiliated with, or operated by UNHCR
or any UN body. Do not present it as an official UNHCR service, and do not use
UNHCR's name or logo as if it were.

The name was chosen partly for this reason: an earlier working title borrowed
UNHCR's own retired site branding, which implied an affiliation that does not
exist.

## Scraping responsibly

`pipeline/build_corpus.py` reads an archive you already hold. If you build that
archive yourself:

- respect `robots.txt` and the site's terms of use
- rate-limit; do not hammer a humanitarian organisation's servers
- re-check content periodically — guidance for refugees goes stale, and stale
  guidance in this domain is not a cosmetic problem

## This tool does not give legal or immigration advice

Päkpätät answers from an archive and cites what it found. It is an
information-retrieval aid for case workers and community members. It is not a
lawyer, not a caseworker, and not a substitute for UNHCR.

Two behaviours exist specifically because of this:

- it refuses rather than guesses when the archive does not cover a question
- it flags answers drawn from retired pages and tells the reader to confirm

Keep both. They are the difference between a useful tool and a dangerous one.

## Personal data

The corpus is built from published public pages, which contain organisational
contact details (office phone numbers, service inboxes). That is public service
information, not personal data about individuals.

Do not extend the corpus with case files, client records, registration numbers,
or anything identifying an individual refugee. Nothing in this pipeline is
designed or audited for that, and the desktop app stores conversation history
in plain `localStorage`.
