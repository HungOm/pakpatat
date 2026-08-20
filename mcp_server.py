#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
MCP server exposing the refugee archive as tools for Claude Desktop, Claude
Code, or any other MCP client.

Two tools, deliberately separated:

  search_archive  -- pure retrieval. Returns the raw archived passages with
                     source URLs. NO language model is involved, so it needs no
                     API key and cannot hallucinate: what you get back is
                     literally text from the archive.

  ask_archive     -- runs the full LangGraph pipeline (retrieve -> guard ->
                     generate -> verify) and returns a cited answer. Needs an
                     API key for whichever provider is configured.

Register in Claude Desktop's config (adjust the paths):

  "mcpServers": {
    "refugee-archive": {
      "command": "/path/to/pakpatat/.venv/bin/python",
      "args": ["/path/to/pakpatat/mcp_server.py"]
    }
  }
"""
import json

from mcp.server.fastmcp import FastMCP

from pakpatat import factcheck, graph, retrieve

mcp = FastMCP("refugee-malaysia-archive")


@mcp.tool()
def search_archive(query: str, top_k: int = 6,
                   source_filter: str | None = None) -> str:
    """Search the offline UNHCR Malaysia archive and return matching passages
    verbatim, with source URLs. No language model is used, so nothing is
    invented -- results are exact text from the archive.

    Args:
        query: What to look for. English or Burmese both work.
        top_k: How many passages to return (default 6).
        source_filter: None for both archives, "new" for the current
            help.unhcr.org/malaysia site only, "old" for the retired
            refugeemalaysia.org site only.
    """
    results = retrieve.search(query, top_k=top_k, source_filter=source_filter)
    if not results:
        return "No matching passages found in the archive."

    out = []
    for r in results:
        currency = ("CURRENT (live UNHCR site)"
                    if r["source"] == "new_site_help_unhcr_org"
                    else "RETIRED OLD SITE - may be out of date, verify by phone")
        heading = f" > {r['section_heading']}" if r.get("section_heading") else ""
        out.append(
            f"## {r['doc_title']}{heading}\n"
            f"Status: {currency}\n"
            f"Source: {r['url']}\n"
            f"Local copy: {r['doc_path']}\n"
            f"Relevance: {r['score']}\n\n{r['text']}"
        )
    return "\n\n---\n\n".join(out)


@mcp.tool()
def ask_archive(question: str, source_filter: str | None = None) -> str:
    """Answer a question using ONLY the offline UNHCR Malaysia archive, with
    citations. Refuses to answer rather than guessing when the archive does not
    cover the question. Requires an API key for the configured model provider.

    Args:
        question: The question to answer. English or Burmese both retrieve, but
            the REPLY language depends on the configured provider: the local
            Ollama model answers in English only, the cloud providers answer in
            Burmese too. A Burmese question is answered either way, never
            refused -- see pakpatat/settings.py PROVIDERS[...]["answer_languages"].
        source_filter: None for both archives, "new" for the current site only,
            "old" for the retired site only.
    """
    result = graph.ask(question, source_filter=source_filter)

    parts = [result["answer"]]
    # graph.ask's contract: every consumer reports the downgrade. Placed above
    # the warnings because it explains the answer the caller is already reading.
    if result.get("lang_downgraded"):
        parts.append(
            "\n*(Answered in English: this question was asked in another "
            "language, which the configured model provider is not trusted to "
            "write. The sources below were found from the question as asked. "
            "Switch to a cloud provider for an answer in that language.)*")
    # Loudest signal first: a value that is not in the archive text is the one
    # thing here that can cause direct harm if acted on.
    if result.get("unverified"):
        parts.append("\n**UNVERIFIED VALUES**\n"
                     + factcheck.describe(result["unverified"]))
    if result["warnings"]:
        parts.append("\n**Warnings**\n" + "\n".join(f"- {w}" for w in result["warnings"]))
    if result["sources"]:
        lines = []
        for s in result["sources"]:
            flag = "current" if s["is_current"] else "RETIRED SITE"
            lines.append(f"- [{s['tag']}] ({flag}) {s['title']} — {s['url']}")
        parts.append("\n**Sources**\n" + "\n".join(lines))
    return "\n".join(parts)


@mcp.tool()
def archive_stats() -> str:
    """Report what is in the archive: how many passages, from which sites, and
    which topics are covered. Useful for knowing the limits of what can be
    answered."""
    st = retrieve._load()  # noqa: SLF001 - internal by design, read-only here
    chunks = st["chunks"]
    by_source: dict[str, int] = {}
    titles: dict[str, set] = {}
    for c in chunks:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
        titles.setdefault(c["source"], set()).add(c["doc_title"])

    return json.dumps({
        "total_passages": len(chunks),
        "by_source": by_source,
        "documents_per_source": {k: len(v) for k, v in titles.items()},
        "note": ("'new_site_help_unhcr_org' is the live UNHCR site (current). "
                 "'old_site_refugeemalaysia_org' is the site UNHCR retired on "
                 "2026-07-14 -- kept because it contains topics the new site "
                 "dropped, but details may be stale."),
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
