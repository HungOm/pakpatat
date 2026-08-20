# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
Read/write the app's runtime settings (model provider, model name, API key).

Settings live in .env, which is git-ignored -- so keys entered in
the Settings panel never enter the repository or a chat window.

Writing here also updates os.environ for the running process and resets the
cached model in pakpatat.graph, so a change takes effect on the next question
with no restart.
"""
import os
import re

from . import config

ENV_PATH = config.HOME / ".env"

# Which providers the Settings panel offers, and what each needs.
#
# `answer_languages` -- WHICH REPLY LANGUAGES A PROVIDER MAY BE ASKED TO WRITE.
#
# This is a capability, not a preference, and it is deliberately separate from
# retrieval. Retrieval is provider-independent and Burmese retrieval is good:
# i18n/README.md measured a Burmese sentence at 0.949 against its own English
# translation, and two of the ten gold-set questions in eval/eval_retrieval.py
# are Burmese. So a Burmese question finds the right sources on every provider.
#
# WRITING the answer is the part that depends on the model. A 3B local model
# writing Burmese safety guidance -- detention, hotline numbers, medical cost --
# produces text that is fluent enough to be trusted and wrong in ways a reader
# cannot see. graph.py already carries the evidence that small local models are
# shaky here: node_generate has a whole retry path (and a `lang_drift` warning)
# because qwen drifts out of the requested language unprompted.
#
# So the gate is conservative on purpose and it is one-directional: a provider
# not listed for a language never gets asked to write it. The question is still
# answered -- in English, from the correct Burmese-retrieved sources, with the
# reason stated -- rather than refused. Losing the reply language is a real cost;
# inventing a Burmese sentence about a police hotline is a worse one.
PROVIDERS = {
    "ollama": {
        "label": "Local / offline (Ollama)",
        "default_model": "qwen2.5:3b-instruct",
        "key_env": None,
        "answer_languages": ["en"],
        "help": "Runs entirely on this computer. Free, private, no internet "
                "needed. Requires Ollama to be installed. Answers in English "
                "only -- ask in any language, but the reply comes back in "
                "English.",
    },
    "google_genai": {
        "label": "Google Gemini",
        "default_model": "gemini-2.5-flash",
        "key_env": "GOOGLE_API_KEY",
        "answer_languages": ["en", "my"],
        "help": "Fast and cheap, with a free tier. Questions are sent to "
                "Google. Answers in Burmese as well as English. "
                "Get a key at aistudio.google.com/apikey",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "default_model": "claude-haiku-4-5",
        "key_env": "ANTHROPIC_API_KEY",
        "answer_languages": ["en", "my"],
        "help": "Questions are sent to Anthropic. Answers in Burmese as well "
                "as English. Key from console.anthropic.com",
    },
    "openai": {
        "label": "OpenAI",
        "default_model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
        "answer_languages": ["en", "my"],
        "help": "Questions are sent to OpenAI. Answers in Burmese as well as "
                "English. Key from platform.openai.com",
    },
}

# The script buckets graph._script_of returns, mapped to the codes above. Only
# scripts we have a considered position on appear here; anything else is left
# alone, because silently downgrading a language nobody has assessed would be
# the same mistake in the other direction.
SCRIPT_LANG = {"latin": "en", "myanmar": "my"}


def answer_languages(provider: str | None = None) -> list[str]:
    """Reply languages the given (or current) provider may be asked to write."""
    provider = provider or config.MODEL_PROVIDER
    return list(PROVIDERS.get(provider, {}).get("answer_languages", ["en"]))


def can_answer_in(lang: str, provider: str | None = None) -> bool:
    """May this provider be asked to reply in `lang`? Unknown languages pass.

    Unknown-passes is deliberate. This gate exists to stop a weak model writing
    a language it cannot write; it is not a language allowlist for the app. A
    script we have not assessed is left to the existing drift check in
    graph.node_generate rather than being quietly forced into English.
    """
    return lang not in SCRIPT_LANG.values() or lang in answer_languages(provider)


def _read_env_file() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    data = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        data[k.strip()] = v.strip()
    return data


def _write_env_file(updates: dict[str, str]) -> None:
    """Merge updates into .env, preserving comments and unrelated keys."""
    existing_lines = (ENV_PATH.read_text(encoding="utf-8").splitlines()
                      if ENV_PATH.exists() else [])
    remaining = dict(updates)
    out = []
    for line in existing_lines:
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)  # keys are readable only by this user


def current() -> dict:
    """Current settings, with the key MASKED -- never return a key to the UI."""
    env = _read_env_file()
    provider = os.getenv("ASSISTANT_MODEL_PROVIDER",
                         env.get("ASSISTANT_MODEL_PROVIDER", config.MODEL_PROVIDER))
    model = os.getenv("ASSISTANT_MODEL",
                      env.get("ASSISTANT_MODEL", config.MODEL_NAME))
    key_env = PROVIDERS.get(provider, {}).get("key_env")
    stored = (os.getenv(key_env) or env.get(key_env, "")) if key_env else ""
    return {
        "provider": provider,
        "model": model,
        "has_key": bool(stored),
        "key_hint": (stored[:4] + "…" + stored[-4:]) if len(stored) > 12 else "",
        # The UI hides the Burmese guide lines when "my" is absent, so this must
        # describe the provider `current()` just resolved -- not the process-wide
        # config, which can lag it before the first save().
        "answer_languages": answer_languages(provider),
        "providers": {
            p: {"label": d["label"], "default_model": d["default_model"],
                "needs_key": bool(d["key_env"]), "help": d["help"],
                "answer_languages": list(d["answer_languages"])}
            for p, d in PROVIDERS.items()
        },
    }


def save(provider: str, model: str | None = None, api_key: str | None = None) -> dict:
    """Persist settings and apply them to the running process."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")

    spec = PROVIDERS[provider]
    model = (model or "").strip() or spec["default_model"]

    updates = {"ASSISTANT_MODEL_PROVIDER": provider, "ASSISTANT_MODEL": model}
    if spec["key_env"] and api_key and api_key.strip():
        updates[spec["key_env"]] = api_key.strip()

    _write_env_file(updates)
    for k, v in updates.items():
        os.environ[k] = v

    # Point the live config at the new choice and drop the cached graph/model.
    config.MODEL_PROVIDER = provider
    config.MODEL_NAME = model
    from . import graph
    graph._GRAPH = None

    if provider == "ollama":
        from . import ollama
        ollama.nudge()   # start the local engine now, not at the first question

    return current()


def check_ready() -> tuple[bool, str]:
    """Is the current provider usable? Returns (ok, message-for-the-user)."""
    env = _read_env_file()
    provider = config.MODEL_PROVIDER
    spec = PROVIDERS.get(provider)
    if not spec:
        return False, f"Unknown provider '{provider}'. Open Settings and pick one."

    if spec["key_env"]:
        if not (os.getenv(spec["key_env"]) or env.get(spec["key_env"])):
            return False, (f"{spec['label']} needs an API key. "
                           f"Click Settings and paste one in.")
        return True, ""

    # Ollama: confirm the local server is actually up before a question fails.
    # If it isn't, try to start it ourselves rather than telling the user to go
    # and run a terminal command. This returns immediately either way, so the
    # UI never blocks -- the banner clears itself once the server answers.
    from . import ollama
    if ollama.is_up():
        if not ollama.has_model(config.MODEL_NAME):
            return False, (f"The local AI model '{config.MODEL_NAME}' has not "
                           f"been downloaded yet. Run 'ollama pull "
                           f"{config.MODEL_NAME}' once, then ask again.")
        return True, ""

    if ollama.nudge():
        return False, "Starting the local AI engine — this takes a few seconds…"
    return False, ("Ollama is not installed on this computer. Install it from "
                   "ollama.com/download, or click Settings to use an online "
                   "provider instead.")
