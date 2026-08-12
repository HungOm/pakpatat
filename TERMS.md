# Päkpätät — name and terms

## The name

**Päkpätät** — K'Cho for **owl**. The one who is asked, and who answers.

Written **Päkpätät**. In identifiers where diacritics do not survive — package
name, repository, CLI, module, environment variables — it is **`pakpatat`**.
Unlike the name it replaced, that ASCII form needs no footnote: it collides
with nothing in the dictionary, so the brand and the identifier are the same
string everywhere.

**Evidence status — read this before citing it.** `päkpätät` is *not* in
K'Cho–English Dictionary v1.0: no `owl` entry in the reverse index, no matching
headword, and zero occurrences in either released corpus. Both corpora are
Bible-derived, and the same gap is already recorded for `hospital`, `police`,
`arrest`, `phone`, `card` and `refugee` — everyday vocabulary a scriptural
corpus does not contain. The evidence here is a **T0 author verdict by a native
speaker**, which is the primary tier in this project's own methodology, not a
weaker substitute for a corpus count. The form is **dialectal** ("in *some*
K'Cho"), and per ruling D-007 a form attested in one lect is not automatically
pan-dialect — that question is still open. See [BRAND.md](BRAND.md) §2.

The two diaereses mark vowel *quality*, not tone. They are letters. Do not
delete them, and do not set the name as `Pakpatat` anywhere a diaeresis will
render.

K'Cho is a Chin language of southern Chin State, Myanmar, spoken by many of the
refugees this tool is built to serve. The name is theirs, not decoration.

**One-line description:** *Offline assistant that answers refugee-support
questions from UNHCR's published guidance, with every answer cited and every
phone number checked. Not affiliated with UNHCR.*

**Tagline:** *It answers what it knows. It says so when it doesn't.*

The former name, **Shümthein** (`m'shüm thein`, "to guide and tell"), and the
full argument for replacing it are kept in [BRAND.md](BRAND.md) §2 — the
etymology was sound; the writing of it was not.

## Terms used throughout the code and docs

Use these words consistently. Where a term has a precise meaning, it is because
something in the code depends on it.

| term | meaning |
|---|---|
| **Archive** | The operator's local corpus of source pages. Never shipped — see NOTICE.md. |
| **Corpus** | `data/corpus.jsonl`: the archive after chunking, one JSON object per chunk. |
| **Chunk** | One retrievable passage, ≤ 220 words, carrying its page title, section heading, URL and currency. |
| **Source card** | A cited source as the reader sees it: tag, title, link, and a *current* / *earlier* badge. |
| **Currency** | Whether a chunk came from the live site (*current*) or a retired one (*earlier*). Retired content always warns. |
| **Refusal gate** | The pre-generation check (`MIN_SCORE`). Below it the model is never called, so it cannot invent. |
| **Weak match** | Above the gate but below `WEAK_SCORE`. Answered, but flagged to the reader. |
| **Grounding** | The model sees only retrieved archive text and is instructed to answer solely from it. |
| **Citation verification** | Post-generation code check that every `[S#]` the model emitted actually exists. Invented ones are stripped. |
| **Fact verification** | Post-generation check that phone numbers, emails, fees and dates in the answer appear verbatim in the retrieved text. |
| **Coverage-adaptive fusion** | BM25's weight scales with how many query words exist in the corpus vocabulary. Protects non-Latin queries; provably a no-op for English. |
| **RRF** | Reciprocal Rank Fusion — combines dense and sparse results by rank, not by score magnitude. |
| **Gold set** | The verified question/fact pairs in `eval/eval_retrieval.py`. The contract retrieval must satisfy. |
| **Baseline** | `eval/baseline.json` — the recorded gold-set result a change is diffed against. |
| **Guide** | The in-app "What can I ask?" browser. Every question in it is verified to retrieve, in both languages. |

## Words to avoid

- **"Refugee Malaysia"** — UNHCR's retired site brand. Implies affiliation we do
  not have. Never use it as a product name.
- **"UNHCR" as the first word of a description.** *"UNHCR refugee help
  assistant"* is a stack of nouns with no head: it reads as *made by UNHCR* or
  *for UNHCR* just as readily as the true reading. Attribution is not
  affiliation — say the answers come *from UNHCR's published guidance*, and put
  the disclaimer in the same sentence. See [BRAND.md](BRAND.md) §4.
- **"Database"** for the index. It is a flat vector array plus BM25, chosen
  deliberately over a database; calling it one invites the wrong changes.
- **"Hallucination"** in user-facing text. Say what actually happened: *"the
  answer cited a source that does not exist"*, *"a number in this answer is not
  in the sources"*. Case workers need the specific failure, not the jargon.
- **"AI-powered"** as a selling point. The trustworthy properties here are
  refusal, citation and verification — not the model.
