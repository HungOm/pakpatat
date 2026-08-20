"""
The LangGraph pipeline:

    retrieve --+--> condense --> reretrieve --+--> generate --> verify --> END
               |                              |
               +------------------------------+--> refuse --> END

Four independent anti-hallucination layers, none of which rely on the model
choosing to behave (which is why this is a graph and not just a prompt):

  1. GUARD (code, pre-generation)
     If the best retrieval score is under config.MIN_SCORE, we never call the
     model at all -- we return "not in the archive". A model that is never
     asked cannot invent an answer. This is the single highest-value gate.

  2. GROUNDING (prompt + closed input set)
     The model only ever sees retrieved archive text, and is instructed to
     answer solely from it and cite [S#] for every claim.

  3. VERIFY CITATIONS (code, post-generation)
     Every [S#] the model emitted is checked against the IDs actually
     retrieved. Invented citations are stripped and the answer is flagged.
     This catches the classic failure where a model cites [S7] when only 6
     sources exist.

  4. VERIFY FACTS (code, post-generation -- pakpatat/factcheck.py)
     Layer 3 proves a citation POINTS somewhere real; it cannot prove the
     number attached to it was copied correctly. So every phone number, email,
     fee and date in the answer must also appear verbatim in the text the model
     was shown. A correctly-cited hotline with one digit changed is the most
     dangerous output this system can produce, and it is the only layer that
     catches it.

The condense step exists because retrieval is stateless. A follow-up like "how
much does it cost?" carries its subject in the previous turn, and embedding
that fragment on its own retrieves noise. See node_condense.

Provider-agnostic by design: the model is constructed via init_chat_model, so
Gemini / Claude / OpenAI / local Ollama all work with no code change.
"""
import datetime as dt
import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from . import config, factcheck, retrieve, settings

SYSTEM_PROMPT = """You are a careful information assistant for a refugee \
community-based organisation (CBO) in Malaysia. You answer questions using ONLY \
an offline archive of UNHCR Malaysia's public information.

ABSOLUTE RULES:
1. Use ONLY the numbered sources provided below. If the sources do not contain \
the answer, say so plainly. NEVER use outside knowledge, and NEVER guess.
2. Cite a source for every factual claim, using its tag: [S1], [S2], etc. Only \
cite tags that actually appear below.
3. NEVER invent or alter a phone number, email address, fee, date, eligibility \
rule, or office hour. Reproduce them EXACTLY as written in the sources. If a \
detail is not in the sources, say it is not in the archive.
4. If a source is marked "FROM THE RETIRED OLD SITE", you MUST warn the reader \
that this information came from a site UNHCR shut down and may be out of date, \
and suggest they verify it by phone.
5. If sources disagree, say so and prefer the one marked CURRENT. A source \
marked SUPERSEDED has been replaced -- report what the CURRENT source says, and \
mention the old answer only to say it has changed.
6. NEVER AGREE WITH A QUESTION THAT THE SOURCES CONTRADICT. A question may \
state something as fact that is out of date or simply wrong ("Why is X not \
covered?", "Why does X cost Y?"). Check the assumption against the sources \
before answering. If they contradict it, open by saying so plainly -- "That has \
changed" or "That is not what the current information says" -- then give the \
correct answer with its citation. Do NOT repeat the question's assumption as \
though a source supported it, and do NOT cite a source as agreeing with a claim \
it contradicts. Use that opening ONLY when a source genuinely contradicts the \
question. If the question assumes nothing false, just answer it normally.
7. ANSWER EVERY PART OF THE QUESTION. If the person asks two things (whether \
something is covered AND what it costs), answer both, or say plainly which part \
is not in the sources.
8. If a source says a service is UNAVAILABLE, SUSPENDED, PAUSED or affected by \
a technical problem, say that FIRST, before any instructions. Steps for using a \
service that is currently down waste a journey or a day's wages.
9. LOCATIONS: when the question is about where to go -- a clinic, hospital, \
office, shelter or service point -- you MUST list each place by NAME together \
with its FULL ADDRESS and its phone number, exactly as written in the sources. \
Never answer a "where" question with only "contact them directly" or "they have \
several locations" when the sources contain addresses. Someone who is ill needs \
the street, not a referral to look it up. If the sources give opening hours or \
the languages spoken, include those too. BUT if the sources genuinely hold no \
addresses -- refugee learning centres, for example, are not listed individually \
-- say so in one sentence and stop. Do NOT pad the answer with source text to \
look like a list of places.
10. WHERE TO GO ONLINE: when the answer tells someone to do something on a \
website, portal or online form, say WHERE first -- name the page and give its \
web address, copied EXACTLY from the sources: a link written in the source \
text, or failing that the source's own "URL:" line. Then give the steps to \
take on that page, in order: what to click, what to type, what to choose. \
"Click on Create a new account" with no address sends someone off to search \
for the page themselves. Never invent, shorten or tidy a link. If the sources \
hold no link for the page, give the steps and say the archive does not hold \
the page's address.
11. FORMATTING: use markdown. Use "###" for section headings, "-" for lists, \
and a markdown table when comparing two or more options across the same fields \
(prices, plans, locations). Put each clinic or office on its own line. Do not \
write a wall of prose when the answer is a list.
12. WRITE AN ANSWER, DO NOT REPRINT THE SOURCES. Never use a citation tag as a \
heading ("### [S7]"), and never paste a source's text as a section. Cite with \
[S7] inside your own sentence. If a source is not relevant to the question, \
ignore it completely -- do not include it to fill space. An answer that repeats \
the archive back is not an answer, and it hides the parts that matter.

{{LANGUAGE_RULE}}

STYLE: Write plainly, for someone whose first language may not be English. \
Short sentences. Lead with the direct answer. Keep numbers and names exact.

CITATIONS: Write each citation as its own bracket, like [S1] [S3]. Do not \
group them like [S1, S3].

Remember: for the people asking, a wrong phone number or a made-up rule can \
cause real harm. Saying "that is not in the archive" is always better than \
guessing."""

# --- The LANGUAGE rule is a slot, not a constant -------------------------------
#
# Which languages the answer may be WRITTEN in depends on the provider, not on
# the question -- see settings.PROVIDERS[...]["answer_languages"] for why a 3B
# local model is not asked to write Burmese safety guidance.
#
# The slot sits where the rule already was, among the other numbered rules,
# rather than being appended after STYLE. Two contradictory language
# instructions in one prompt is precisely how a small model ends up obeying
# whichever it read last, so there is only ever ONE language rule in the prompt.
_LANGUAGE_SLOT = "{{LANGUAGE_RULE}}"

_LANG_MIRROR = (
    "LANGUAGE: Reply in the SAME language the question was written in. An "
    "English question gets an English answer; a Burmese question gets a Burmese "
    "answer. Never switch to a language the user did not use. Keep phone "
    "numbers, email addresses and proper names in their original form, "
    "unchanged."
)

# Note the "do not apologise for it" clause. Without it the model opens with a
# paragraph explaining that it cannot write Burmese, which buries the answer
# under an apology for a decision the app made -- and says it in English to
# someone who may not read English. The app states the reason itself, once, as
# a warning attached to the answer (node_verify).
_LANG_ENGLISH_ONLY = (
    "LANGUAGE: Write your entire reply in ENGLISH, whatever language the "
    "question was written in. Do not write any Burmese. Do not apologise for "
    "answering in English, do not offer to translate, and do not mention the "
    "language of the question at all -- this is expected, and the reader is "
    "told why separately. Keep phone numbers, email addresses and proper names "
    "in their original form, unchanged."
)


def system_prompt(mirror_language: bool = True) -> str:
    """SYSTEM_PROMPT with the one applicable language rule filled in."""
    return SYSTEM_PROMPT.replace(
        _LANGUAGE_SLOT, _LANG_MIRROR if mirror_language else _LANG_ENGLISH_ONLY)

# Written for the person asking, who may be frightened and may not read English
# easily. No internal file paths, no jargon: a repo path is meaningless to
# someone in a police station, and "archive" is not a word they used.
# Phrases a help site uses when something is switched off. Kept narrow and
# literal: this fires a prominent warning, so a loose pattern that matched
# ordinary coverage text ("not covered") would train staff to ignore it.
_OUTAGE_RE = re.compile(
    r"currently unavailable|temporarily unavailable|not available at this time"
    r"|temporarily (?:suspended|closed|paused)|service is suspended"
    r"|suspended until further notice|ongoing (?:global )?technical (?:issue|problem)",
    re.I,
)

REFUSAL = (
    "I could not find an answer to this in the official UNHCR information I "
    "have, so I will not guess.\n\nI can help with: registering with UNHCR, "
    "refugee status, health care and REMEDI insurance, education, child "
    "protection, gender-based violence, legal help, arrest and detention, "
    "resettlement and going home, avoiding scams, and the My Services "
    "portal.\n\nTry asking in a different way. For anything urgent, contact "
    "UNHCR directly by phone."
)


class State(TypedDict, total=False):
    question: str
    history: list[dict]
    search_query: str | None
    lang_drift: bool
    lang_downgraded: bool
    source_filter: str | None
    results: list[dict]
    top_score: float
    answer: str
    sources: list[dict]
    refused: bool
    unverified: list[dict]
    warnings: list[str]


CONDENSE_PROMPT = """Rewrite the FOLLOW-UP QUESTION so that it can be \
understood on its own, by someone who has not seen the conversation.

Replace words like "it", "that", "there" and "they" with the thing they refer \
to in the conversation above. Keep the original wording and language wherever \
you can. Do NOT answer the question. Do NOT add any information that is not \
already in the conversation.

If the follow-up question already makes sense on its own, repeat it back \
completely unchanged.

Reply with the rewritten question ONLY -- one line, no explanation, no label."""


_MODEL = None
_MODEL_KEY = None


def _model():
    """Build the chat model from config. Kept lazy so that pure-search use
    (the MCP search tool) never needs an API key at all.

    Cached: rebuilding this per question re-does provider setup for no gain.
    The cache key is the provider+model, so switching in Settings still takes
    effect immediately."""
    global _MODEL, _MODEL_KEY
    missing = config.key_missing()
    if missing:
        raise SystemExit(
            f"\nNo API key found for provider '{config.MODEL_PROVIDER}'.\n"
            f"Set {missing} in .env\n"
            f"(Copy .env.example to .env and paste your key in.)\n"
        )

    key = (config.MODEL_PROVIDER, config.MODEL_NAME, config.MODEL_TEMPERATURE)
    if _MODEL is not None and _MODEL_KEY == key:
        return _MODEL

    from langchain.chat_models import init_chat_model

    kwargs = {}
    if config.MODEL_PROVIDER == "ollama":
        # Hold the weights in memory while the app is open. The default is 5
        # minutes, so anyone asking questions occasionally pays a full model
        # reload -- painful on a machine that is already short on RAM.
        kwargs["keep_alive"] = config.OLLAMA_KEEP_ALIVE
        # Without this, Ollama caps the prompt at 4096 tokens and silently
        # drops the overflow -- see config.NUM_CTX. Measured prompts here reach
        # ~4.3k, so sources were being cut off mid-answer with no warning.
        kwargs["num_ctx"] = config.NUM_CTX

    _MODEL = init_chat_model(
        config.MODEL_NAME,
        model_provider=config.MODEL_PROVIDER,
        temperature=config.MODEL_TEMPERATURE,
        **kwargs,
    )
    _MODEL_KEY = key
    return _MODEL


# ------------------------------------------------------------------ graph nodes
def _run_retrieval(query: str, source_filter: str | None) -> tuple[list[dict], float]:
    results = retrieve.search(query, source_filter=source_filter)
    # Gate on the best ABSOLUTE similarity across all returned chunks, not on
    # the fused ranking score -- see the note in config.MIN_SCORE.
    return results, max((r["raw"] for r in results), default=0.0)


def node_retrieve(state: State) -> State:
    results, top_raw = _run_retrieval(state["question"],
                                      state.get("source_filter"))
    return {"results": results, "top_score": top_raw}


def _should_condense(state: State) -> bool:
    """Decide whether this question needs the previous turn to make sense.

    Rewriting costs a model call, which on a local 7B is felt, so it is not run
    on every turn. Two cheap signals, either of which is enough:

      - WEAK RETRIEVAL. A self-contained question generally finds its page.
        "how much does it cost?" embeds to nothing in particular and scores
        near the floor, which is exactly the config.WEAK_SCORE band.
      - A VERY SHORT QUESTION. Follow-up fragments are short; full questions
        rarely are. Measured on non-space characters rather than words, because
        Burmese is not word-spaced and a word count would read every Burmese
        question as a single word.

    Over-triggering is cheap: the condense prompt returns a self-contained
    question unchanged, and node_reretrieve throws the rewrite away unless it
    retrieves better. Under-triggering silently answers the wrong question, so
    the bias here is deliberately towards running it.
    """
    if not state.get("history"):
        return False
    compact = "".join(state["question"].split())
    return (state.get("top_score", 0.0) < config.WEAK_SCORE
            or len(compact) < 30)


def node_condense(state: State) -> State:
    """Rewrite a follow-up into a standalone search query.

    Retrieval-only: node_generate still receives the question the person
    actually typed, so the answer addresses their words in their language. The
    rewrite exists purely to give the embedder and BM25 something with a
    subject in it.
    """
    convo = []
    for m in (state.get("history") or [])[-4:]:
        who = "User" if m.get("role") == "user" else "Assistant"
        text = " ".join((m.get("text") or "").split())
        if len(text) > 300:                 # previous answers are long; the
            text = text[:300] + "..."       # subject is always near the start
        if text:
            convo.append(f"{who}: {text}")
    if not convo:
        return {}

    prompt = (
        f"{CONDENSE_PROMPT}\n\n"
        f"=== CONVERSATION SO FAR ===\n" + "\n".join(convo) + "\n\n"
        f"=== FOLLOW-UP QUESTION ===\n{state['question']}\n\n"
        f"Rewritten question:"
    )
    try:
        response = _model().invoke(prompt)
    except (Exception, SystemExit):
        # Rewriting is an optimisation. If the model is unreachable, let the
        # original question through and fail (or not) in node_generate, where
        # the error message is already written for a non-technical reader.
        return {}

    raw = response.content if isinstance(response.content, str) else str(response.content)

    # Small models like to explain themselves. Take the first real line and
    # strip the label or quotes they tend to wrap it in.
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    line = re.sub(r'^(?:rewritten\s+question|question)\s*:\s*', "", line, flags=re.I)
    line = line.strip().strip('"“”')

    # No script check on the rewrite: an English rewrite of a Burmese follow-up
    # is fine, and often retrieves better, because the corpus is English and
    # retrieval is cross-lingual by design.
    if not line or len(line) > 300:
        return {}
    return {"search_query": line}


def node_reretrieve(state: State) -> State:
    """Search again with the rewritten query, and keep it only if it did
    better. A bad rewrite therefore costs latency and nothing else."""
    query = (state.get("search_query") or "").strip()
    if not query or query == state["question"].strip():
        return {}

    results, top_raw = _run_retrieval(query, state.get("source_filter"))
    if top_raw <= state.get("top_score", 0.0):
        return {"search_query": None}
    return {"results": results, "top_score": top_raw}


def _threshold_for(question: str) -> float:
    """One flat threshold for every language. See config.MIN_SCORE for why a
    script-aware variant was measured and rejected."""
    return config.MIN_SCORE


def guard(state: State) -> Literal["generate", "refuse"]:
    """The pre-generation gate. Below threshold, the model is never called,
    so it has no opportunity to invent an answer."""
    if not state["results"]:
        return "refuse"
    if state["top_score"] < _threshold_for(state["question"]):
        return "refuse"
    return "generate"


def route_after_retrieve(state: State) -> Literal["condense", "generate", "refuse"]:
    """Send follow-up questions through the rewrite before judging them.

    Without this, the refusal gate sees "how much does it cost?" scoring below
    MIN_SCORE and reports that the archive has nothing on it -- one turn after
    quoting the page that answers it.
    """
    if _should_condense(state):
        return "condense"
    return guard(state)


def node_refuse(state: State) -> State:
    return {"answer": REFUSAL, "sources": [], "refused": True, "warnings": []}


def _script_of(text: str) -> str:
    """Coarse script bucket for language-drift detection."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "unknown"
    buckets = {"latin": 0, "myanmar": 0, "cjk": 0, "other": 0}
    for c in letters:
        o = ord(c)
        if o < 0x250:
            buckets["latin"] += 1
        elif 0x1000 <= o <= 0x109F:
            buckets["myanmar"] += 1
        elif 0x3000 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF:
            buckets["cjk"] += 1
        else:
            buckets["other"] += 1
    return max(buckets, key=buckets.get)


def _reply_language(question: str) -> tuple[str, bool]:
    """Which script the reply should be in, and whether that is a downgrade.

    Retrieval already ran and is provider-independent, so the sources are the
    right ones either way -- this decides only what language they are written up
    in. Returns (wanted script bucket, downgraded), where `downgraded` means the
    question's own language was available in the archive but not from the model
    the operator has selected.
    """
    asked = _script_of(question)
    lang = settings.SCRIPT_LANG.get(asked)
    if lang and not settings.can_answer_in(lang):
        return "latin", True
    return asked, False


def node_generate(state: State) -> State:
    results = state["results"]

    # Decide the reply language BEFORE building the prompt, so the prompt carries
    # exactly one language rule (see _LANGUAGE_SLOT).
    want, downgraded = _reply_language(state["question"])
    base = (
        f"{system_prompt(mirror_language=not downgraded)}\n\n"
        f"=== SOURCES FROM THE ARCHIVE ===\n{retrieve.format_sources(results)}\n\n"
        f"=== QUESTION ===\n{state['question']}\n\n"
        f"Answer using only the sources above, citing [S#] for each claim."
    )
    model = _model()
    response = model.invoke(base)
    text = response.content if isinstance(response.content, str) else str(response.content)

    # Enforce reply language in code, not just by instruction. Smaller local
    # models (qwen, llama) drift into their training language on some prompts
    # -- an English question answered in Chinese is useless and alarming to the
    # person asking, so retry once with a hard directive.
    #
    # `want` is the language we ASKED for, which after a downgrade is English
    # rather than the question's own script. Checking against the question here
    # would flag the downgrade itself as drift and retry the model into writing
    # the very language the provider was ruled out of writing.
    got = _script_of(text)
    if want != "unknown" and got != "unknown" and want != got:
        lang = {"latin": "English", "myanmar": "Burmese",
                "cjk": "the language of the question"}.get(want, "the question's language")
        retry = model.invoke(
            f"{base}\n\nCRITICAL: Your entire reply MUST be written in {lang}. "
            f"Do not use any other language."
        )
        retry_text = retry.content if isinstance(retry.content, str) else str(retry.content)
        if _script_of(retry_text) == want:
            text = retry_text
        else:
            return {"answer": text, "refused": False, "lang_drift": True,
                    "lang_downgraded": downgraded}

    return {"answer": text, "refused": False, "lang_downgraded": downgraded}


def node_verify(state: State) -> State:
    """Post-generation check: every [S#] cited must exist in what we retrieved.

    An invented citation is the loudest possible signal that the model went
    outside its sources, so we strip it and flag the answer rather than
    letting a confident-looking fake reference through.
    """
    answer = state["answer"]
    n_sources = len(state["results"])
    valid = {f"S{i}" for i in range(1, n_sources + 1)}

    # Match citations inside any bracket group, so grouped forms like
    # "[S1, S3]" and "[S2; S4]" are read correctly. A stricter pattern would
    # silently score a well-cited answer as "cited nothing" and fire a
    # misleading warning.
    # Repair the tags the model mangles, BEFORE reading them.
    #
    # Measured on qwen2.5:3b: answers came back citing "[SD1]" and "[SD3]" --
    # never "SD" anywhere in the prompt, the model simply invented the letter.
    # Because those are not valid tags, a properly grounded answer was scored as
    # "cited no sources" and carried a warning telling staff not to trust it.
    # A warning that fires on a correct answer is worse than none: it teaches
    # people to ignore the one that matters.
    #
    # Only the DIGIT is trusted, and only when it indexes a source that was
    # actually retrieved -- so a repaired tag is still checked against `valid`
    # below exactly like any other. Bracketed junk with no digit at all
    # ("[REMEMBER]", "[REDACTED]", "[END OF SOURCES]" -- all observed) is
    # removed, since it reads as a citation to anything it points at.
    def _repair(m: re.Match) -> str:
        inner = m.group(1)
        if re.fullmatch(r"\s*S\d+\s*", inner):
            return f"[{inner.strip()}]"
        if (d := re.fullmatch(r"\s*(?:S[A-Z]|source|src)\s*(\d+)\s*", inner, re.I)):
            n = d.group(1)
            return f"[S{n}]" if f"S{n}" in valid else ""
        if re.fullmatch(r"[A-Z][A-Z ]{2,}", inner):      # ALL-CAPS pseudo-tag
            return ""
        return m.group(0)

    answer = re.sub(r"\[([^\]\n]{1,40})\]", _repair, answer)
    state["answer"] = answer

    cited: set[str] = set()
    for group in re.findall(r"\[([^\]]*S\d+[^\]]*)\]", answer):
        cited.update(re.findall(r"S\d+", group))

    warnings: list[str] = []

    invented = cited - valid
    if invented:
        # Remove only the invented ID, preserving any valid ones sharing the
        # same bracket group.
        for bad in sorted(invented):
            answer = re.sub(rf"\b{bad}\b[,;]?\s*", "", answer)
        answer = re.sub(r"\[\s*[,;]*\s*\]", "", answer)      # drop emptied brackets
        answer = re.sub(r"\[([^\]]*?)[,;]\s*\]", r"[\1]", answer)  # trim "[S1, ]"
        warnings.append(
            f"Removed {len(invented)} citation(s) pointing to sources that do "
            f"not exist ({', '.join(sorted(invented))}). Treat this answer with "
            f"extra caution and verify it against the linked pages."
        )

    used = cited & valid
    if not used:
        warnings.append(
            "The answer cited no sources. It may not be grounded in the "
            "archive -- verify before sharing it."
        )

    # Report the sources the answer actually leaned on. If it cited nothing,
    # fall back to showing everything retrieved so the reader can still check.
    report_all = not used
    sources = []
    shown = []            # the raw records behind `sources`, for the warnings
    for n, r in enumerate(state["results"], 1):
        tag = f"S{n}"
        if not report_all and tag not in used:
            continue
        shown.append(r)
        sources.append({
            "tag": tag,
            "title": r["doc_title"],
            "section": r.get("section_heading"),
            "url": r["url"],
            # Current means "safe to act on today", which is not the same as
            # "came from the live website". Material UNHCR handed to this
            # organisation directly is current too -- and a live-site chunk that
            # newer material has superseded is not. Keying this off the source
            # name alone badged the newest source in the archive as old and
            # fired the retired-site warning on answers that never touched the
            # retired site.
            "is_current": (r["source"] in ("new_site_help_unhcr_org",
                                           "partner_materials")
                           and (r.get("status") or "").lower() != "superseded"),
            "score": r["score"],
            "doc_path": r["doc_path"],
            # Shown in place of the link for sources that are documents rather
            # than web pages, so the reader sees which minute an answer rests on.
            "citation": r.get("citation"),
            "source_document": r.get("source_document"),
        })

    if any(r["source"] == "old_site_refugeemalaysia_org" for r in shown):
        warnings.append(
            "This answer draws on the RETIRED refugeemalaysia.org site. UNHCR "
            "took that site down on 2026-07-14, so details may have changed -- "
            "confirm by phone before acting on them."
        )
    # SERVICE-STATUS LAYER (code, not prompt).
    #
    # Measured: asked "how do I create a My Services account?", the retrieved
    # sources included "creation of new My Services accounts is currently
    # unavailable" at ranks 6 and 8 -- inside the window, in the prompt -- and
    # BOTH qwen2.5:3b and 7b wrote a confident step-by-step answer without
    # mentioning it. Adding a rule to SYSTEM_PROMPT did not fix it either; a
    # small model attends poorly to source 6 of 8.
    #
    # So this does not ask the model. If the archive says a service is down and
    # the answer does not say so, the warning is attached in code. Steps for a
    # service that is currently unavailable cost someone a journey and a day's
    # pay.
    outage = [r for r in shown if _OUTAGE_RE.search(r["text"])]
    if outage and not _OUTAGE_RE.search(answer):
        warnings.insert(0, (
            "SERVICE MAY BE UNAVAILABLE. The archive says this service is "
            "currently affected by an outage or suspension, but the answer "
            "above does not mention it. Read the sources before following "
            "these steps."
        ))

    # TIME-LIMITED FACTS (code, not prompt).
    #
    # A promotional price and a temporary suspension both stop being true on a
    # date, and nothing else here notices a date passing: the corpus has no
    # clock, the nightly refresh watches only the WEBSITE, and material handed
    # over at a meeting is never re-scraped at all. Left alone, the REMEDI child
    # promotion would go on being quoted as current forever. Chunks can now
    # carry `review_by`, and once that date arrives the answer says so.
    _today = dt.date.today()
    for r in shown:
        rb = r.get("review_by")
        if not rb:
            continue
        try:
            due = dt.date.fromisoformat(rb)
        except ValueError:
            continue
        if _today >= due:
            warnings.insert(0, (
                f"TIME-LIMITED: part of this answer was only confirmed until "
                f"{rb}, which has now passed. Re-confirm with UNHCR or Allianz "
                f"before telling anyone -- a promotional price or temporary "
                f"arrangement may have ended."))
        elif (due - _today).days <= 14:
            warnings.append(
                f"Part of this answer is time-limited and must be re-confirmed "
                f"by {rb}.")
        break

    if any((r.get("status") or "").lower() == "superseded" for r in shown):
        warnings.append(
            "Part of the archive used here has been SUPERSEDED by newer "
            "information UNHCR provided directly. Trust the newer source, and "
            "confirm anything that matters by phone."
        )

    # Weak-match band: above the refusal gate but not a confident hit. This is
    # the safety net for questions in languages where cross-lingual similarity
    # scores run lower than English (see config.WEAK_SCORE).
    if state.get("lang_drift"):
        warnings.append(
            "The AI replied in the wrong language despite being asked twice. "
            "This happens with smaller local models -- switch to an online "
            "provider in Settings if it keeps happening."
        )

    if state.get("top_score", 0.0) < config.WEAK_SCORE:
        warnings.append(
            "The archive did not match this question strongly. The answer may "
            "be incomplete or off-target -- read the linked sources yourself "
            "before relying on it."
        )

    # Hard facts must appear verbatim in what the model was shown. The haystack
    # is the rendered sources PLUS the question: a number the person typed
    # themselves and the model repeated back is not an invention.
    #
    # Checked against ALL retrieved chunks, not just the cited ones. The model
    # sometimes copies a number correctly but attributes it to the wrong [S#];
    # that is a citation error, already caught above, and it should not be
    # escalated into "this number may be fabricated".
    rendered = retrieve.format_sources(state["results"])
    unverified = factcheck.check(answer, f"{rendered}\n{state['question']}")

    # REGURGITATION CHECK (code, not prompt).
    #
    # Observed in the field: an education question came back as a short real
    # answer followed by "### [S7]", "### [S8]", "### [S6]" -- citation tags
    # used as HEADINGS, each with a source block pasted verbatim underneath,
    # including two REMEDI chunks that had nothing to do with schools. The
    # reader cannot tell that from a written answer; it looks authoritative and
    # long, and the actual answer is buried in the first paragraph.
    #
    # A tag-as-heading is never legitimate, so those lines are removed outright.
    # Then, because stripping the heading still leaves the pasted text, the
    # answer is measured against the sources: if most of it is copied verbatim,
    # it is not an answer and the reader is told so. Rule 12 asks the model not
    # to do this; this is what happens when it does anyway.
    answer = re.sub(r"^[ \t]*#{1,6}[ \t]*\[S\d+\][ \t]*$\n?", "", answer,
                    flags=re.M)

    hay = re.sub(r"\s+", " ", rendered)
    lines = [ln.strip() for ln in answer.split("\n") if len(ln.strip()) >= 60]
    if lines:
        copied = sum(len(ln) for ln in lines
                     if re.sub(r"\s+", " ", ln) in hay)
        total = sum(len(ln) for ln in lines)
        if total and copied / total > 0.5:
            warnings.append(
                "Most of this reply is archive text copied word-for-word rather "
                "than an answer to your question. Read the sources below "
                "directly, and try asking in a more specific way.")

    return {"answer": answer.strip(), "sources": sources,
            "warnings": warnings, "unverified": unverified}


def build_graph():
    g = StateGraph(State)
    g.add_node("retrieve", node_retrieve)
    g.add_node("condense", node_condense)
    g.add_node("reretrieve", node_reretrieve)
    g.add_node("generate", node_generate)
    g.add_node("verify", node_verify)
    g.add_node("refuse", node_refuse)

    g.add_edge(START, "retrieve")
    g.add_conditional_edges("retrieve", route_after_retrieve,
                            {"condense": "condense",
                             "generate": "generate", "refuse": "refuse"})
    g.add_edge("condense", "reretrieve")
    # The gate runs on whichever retrieval won, so a rewrite that found nothing
    # is still refused rather than answered from noise.
    g.add_conditional_edges("reretrieve", guard,
                            {"generate": "generate", "refuse": "refuse"})
    g.add_edge("generate", "verify")
    g.add_edge("verify", END)
    g.add_edge("refuse", END)
    return g.compile()


_GRAPH = None


def ask(question: str, source_filter: str | None = None,
        history: list[dict] | None = None) -> dict:
    """Answer a question against the archive.

    history: prior turns as [{"role": "user"|"bot", "text": ...}], oldest
    first. Used ONLY to rewrite a follow-up into a standalone search query;
    the answer is always written against the question as asked.

    Returns {answer, sources, warnings, unverified, refused, top_score,
    search_query, lang_downgraded}.

    `lang_downgraded` is True when the question was asked in a language the
    archive can be searched in but the SELECTED PROVIDER is not trusted to write
    (today: Burmese on a local model -- see settings.PROVIDERS). The answer is
    still a real answer from the right sources; only its language changed. Every
    consumer should say so, in the reader's own language.

    NOTE FOR CALLERS: `unverified` is a list of hard facts (phone numbers,
    emails, fees, dates) that do NOT appear in the archive text this answer was
    built from. It is not folded into `warnings`, because it deserves louder
    treatment than a warning -- every consumer must render it. See
    pakpatat/factcheck.describe() for a ready-made sentence.
    """
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    out = _GRAPH.invoke({"question": question, "source_filter": source_filter,
                         "history": history or []})
    return {
        "answer": out["answer"],
        "sources": out.get("sources", []),
        "warnings": out.get("warnings", []),
        "unverified": out.get("unverified", []),
        "refused": out.get("refused", False),
        "top_score": out.get("top_score", 0.0),
        "search_query": out.get("search_query") or None,
        # A FLAG, not a warning sentence, on purpose. The one person who needs
        # to read this is by definition not reading English well -- so it has to
        # be renderable in the UI's own language, and a prose string baked in
        # here can only ever be English. Same pattern as `unverified`: the
        # server reports the fact, the client says it in the reader's language.
        "lang_downgraded": bool(out.get("lang_downgraded", False)),
    }
