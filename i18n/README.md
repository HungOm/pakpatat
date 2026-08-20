# Adding a language

## What is safe to add, and what is not

Päkpätät separates two things that look like one:

| | needs what | status |
|---|---|---|
| **Interface language** — buttons, labels, guide questions | human translation | add freely |
| **Question language** — free-text questions typed by the user | the embedding model must represent the language | **measure first** |
| **Answer language** — the language the reply is written in | the answering model must generate the language | **measure first** |

A language can be fully supported in the interface while being unsupported for
free-text questions. That is not a bug, and it is much better than the
alternative — see below.

## Measure before promising a language

Retrieval works cross-lingually only if the embedding model actually represents
the language. Measured on this corpus with
`paraphrase-multilingual-MiniLM-L12-v2`:

| language | sentence ↔ its own English translation |
|---|---|
| Burmese | **0.949** — genuinely supported |
| a language the model was never trained on | **0.187**, against **0.138** for unrelated pairs — **not supported** |

That second row is a real measurement, not a hypothetical. For that language the
correct translation ranked #1 for only 6 of 60 sentence pairs — barely above
chance. BM25 does not rescue it either: words the English corpus never contains
have ~0 lexical coverage. Any language outside the embedding model's training
will look like this, and the number is the only way to find out which.

**Never ship free-text support for a language without running this test.** A tool
that appears to understand someone and then answers from the wrong page is worse
than one that plainly says "type your question in English or Burmese".

## The guide is not (yet) a bridge — read this before relying on it

An earlier version of this file claimed a guide question is *displayed* in one
language but *asks* a pre-verified English query, so retrieval never sees the
untrained language. **That is not what the code does.**

`ui/index.html` renders each guide line as `data-guide-ask="<the displayed
text>"` and submits exactly that string. Tapping the Burmese line runs a
*Burmese* query. It works for Burmese because Burmese retrieval genuinely works
(0.949 above), not because of any indirection.

So for a language the embedding model does not represent, a guide line in that
language would retrieve at random. **Adding guide questions in such a language
requires building the display/query split first.** The pieces are small (carry a
verified English query beside the display text and submit that), but until they
exist, translating guide questions into an unrepresented language makes the tool
worse, not better.

Interface strings — buttons, labels, headings — are unaffected. They are never
submitted as a query, so they can be translated into any language today.

## Answers depend on the PROVIDER, not just the language

Retrieval is provider-independent. Writing the answer is not, so the reply
language is gated per provider in `pakpatat/settings.py`
(`PROVIDERS[...]["answer_languages"]`):

| provider | writes |
|---|---|
| Local / Ollama (qwen2.5) | English only |
| Gemini · Claude · OpenAI | English, Burmese |

A question in a language the current provider cannot write is **still
answered** — the archive is searched in the language it was asked in, and the
answer comes back in English with `lang_downgraded` set, which the UI renders
as an explanation in the reader's own language. It is never refused: the
sources found are the right ones, and only the write-up language changed.

Consequences worth knowing:

- On the local model the guide and the hero cards list questions in **English
  only**, because a Burmese line there would come back in English every time.
  The Burmese *interface* stays on — that text is human-translated and never
  reaches a model.
- **No provider writes a low-resource language it was not trained on.** Asked
  to try, the local model produced output matching **2 of 12 tokens** against a
  reference dictionary and violating the orthography outright. That is invented
  text wearing the shape of a real language — the exact failure this project
  exists to prevent, and it is worst precisely where a language has fewest
  speakers available to catch it.

## Filling in `translation_worksheet.csv`

**133 rows**, cut directly from the `STR` and `GUIDE_EN` tables in
`ui/index.html` and verified to round-trip against them — every key the app
holds is present, nothing is invented, and the `english` / `burmese` cells are
byte-identical to what ships.

| section | rows | |
|---|---|---|
| `ui` | 65 | interface strings |
| `guide_category` | 11 | category headings |
| `guide_question` | 49 | the guide itself |
| `hero_card_label` | 4 | the four hero cards |
| `hero_card_question` | 4 | their question text |

Categories are interleaved with their own questions, so a translator sees each
question in the context of its heading.

### Start with the `notes` column — not every row is safe to use

Sort by `notes`. Each row is prefixed with its status:

- **`SAFE` (79 rows)** — interface text and category headings. These are
  *never* submitted as a query, so they can be translated and shipped today.
  For a language that fails the retrieval measurement, this is the whole
  supported path — and it is worth doing, because it still makes the app
  navigable in that language.
- **`BLOCKED` (53 rows)** — every guide question and hero-card question.
  Translating them is fine; **enabling them is not**, because `ui/index.html`
  submits the tapped text as the real query. In a language that failed the
  measurement above, a question here would retrieve at random. These become
  usable only once the display/query split described earlier exists.
- **`SKIP` (1 row)** — `modeCloud` is a code template, not a sentence. Ask
  before touching it.

### The columns

- `english`, `burmese` — reference, already verified
- `translation_TO_FILL` — the column to complete
- `notes` — status prefix plus the reason, as above

Please have a native speaker do this. For a low-resource language, machine
translation is not available at usable quality — measured, the local model
matched **2 of 12 tokens** against a reference dictionary — and a
plausible-looking wrong word in a safety context (detention, violence, medical
cost) causes real harm.

### Re-cutting it after the UI changes

The worksheet is generated, not maintained by hand, and it *will* drift again
the next time a guide question is added. Re-cut it from `ui/index.html` rather
than editing rows in place, and diff the result: a question that quietly
vanished from the sheet is a question that comes back untranslated with nothing
to show it was ever missing.

