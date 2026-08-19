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

**This software fetches from `help.unhcr.org` itself.** Not only as an operator
script: the desktop app's Knowledge base panel will crawl the site from a
button, and the person pressing it may be a case worker who has never read this
file. That is a deliberate choice — an install with no archive was previously a
dead end for anyone without a developer — but it means the politeness is the
software's responsibility, not the reader's good intentions.

So it is enforced in code, in `pakpatat/scrape.py`, on every request this
project makes:

- `robots.txt` is fetched and obeyed, and a site whose `robots.txt` cannot be
  read is treated as forbidden rather than permitted
- a real rate limit with jitter between requests, and a site-declared
  `Crawl-delay` is honoured when it is slower than ours
- conditional requests (`If-None-Match` / `If-Modified-Since`), so re-checking
  an unchanged page costs a `304` and no body
- exponential backoff honouring `Retry-After` on `429` / `503`
- a hard per-run request budget, so a bug in a loop cannot become a flood
- a `User-Agent` that says what the client is and how to make it stop

If you fork this and point it at a different site, those guarantees are yours
to keep. Do not remove them, and do not raise the rate limit to make a crawl
finish sooner — the servers on the other end belong to a humanitarian
organisation, and nothing this tool does is urgent enough to justify the load.

Whatever you fetch, re-check it periodically: guidance for refugees goes stale,
and stale guidance in this domain is not a cosmetic problem.

Fetching for your own offline use is a different act from republishing. The
section above still governs the second one.

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
