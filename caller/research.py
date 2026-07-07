"""Research phase: gather current evidence before reasoning.

The model's training data is stale by definition for live forecasting
questions, so every forecast starts with retrieval. This module turns a
Question into a short "research digest" — a plain-text bundle of recent,
relevant snippets that gets injected into the reasoning prompt.

Design notes:
  * The backend is pluggable via the `SearchBackend` protocol. Tavily is the
    default because its REST API is a single POST with an API key; swapping
    in Exa, Brave, or AskNews later means implementing one method.
  * Live mode decomposes the question into targeted sub-queries with one
    cheap LLM call (Phase 2) — base rates, key actors, recent developments,
    status-quo indicators. The fixed 3-template approach from Phase 1 is
    kept as the fallback: retrieval should degrade gracefully, never block
    a forecast because the query-generation call hiccuped.
  * MockBackend lets the whole pipeline run offline for testing — it returns
    canned snippets so you can verify plumbing without burning API calls.
"""

import json
import sys
from typing import Protocol

import requests

from .config import Config
from .question import Question

QUERY_PROMPT = """\
You generate web-search queries for a forecasting system. Given a binary
forecasting question, produce 4-6 short search queries that together gather
the evidence a superforecaster needs:

1. At least one query aimed at the HISTORICAL BASE RATE — how often events
   in this reference class have happened before.
2. At least one query aimed at RECENT DEVELOPMENTS — the latest news bearing
   directly on the question.
3. Queries about the KEY ACTORS or institutions whose decisions drive the
   outcome (a central bank, a company, a regulator, a candidate).
4. Where relevant, a query about the CURRENT STATUS-QUO VALUE of whatever
   the question measures (the current rate, price, poll number).

Write queries the way a skilled human would type them into a news search —
short keyword phrases, not full sentences, no boolean operators.

Respond with ONLY a JSON array of query strings, no markdown fences, no
preamble. Example: ["query one", "query two", "query three", "query four"]"""


def generate_queries(question: Question, cfg: Config) -> list[str]:
    """Decompose the question into targeted search queries with one LLM call.

    Uses the cheap query model (Haiku by default) — this is a small structured
    task where the expensive reasoning model would be wasted. Raises on any
    failure; gather() catches and falls back to the fixed templates.
    """
    # Imported lazily so mock mode works without the SDK installed.
    import anthropic
    from datetime import date

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    message = client.messages.create(
        model=cfg.query_model,
        max_tokens=1000,
        system=QUERY_PROMPT,
        messages=[
            {
                "role": "user",
                # The model can't know the current date and will otherwise
                # guess a year from its training data, biasing queries toward
                # stale coverage ("current rate 2024" when it's 2026).
                "content": (
                    f"TODAY'S DATE: {date.today().isoformat()}\n"
                    f"{question.prompt_block()}"
                ),
            }
        ],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")

    cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in query-generation output:\n{raw}")
    data = json.loads(cleaned[start : end + 1])

    queries = [str(q).strip() for q in data if str(q).strip()]
    if not queries:
        raise ValueError(f"Query generation returned an empty list:\n{raw}")
    # Cap defensively — each extra query is another search API call.
    return queries[:6]


class SearchBackend(Protocol):
    def search(self, query: str, max_results: int) -> list[dict]:
        """Return a list of {title, url, content} dicts."""
        ...


class TavilyBackend:
    """Minimal Tavily REST client. https://tavily.com"""

    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError(
                "TAVILY_API_KEY is not set. Get a free key at tavily.com "
                "or run with --mock to test the pipeline offline."
            )
        self.api_key = api_key

    def search(self, query: str, max_results: int) -> list[dict]:
        resp = requests.post(
            self.ENDPOINT,
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": max_results,
                # "news" topic biases toward recent coverage, which is what
                # forecasting needs; the general index is noisier.
                "topic": "news",
            },
            timeout=30,
        )
        if not resp.ok:
            # Tavily puts the reason for a rejection (bad query, bad key,
            # rate limit) in the response body; a bare raise_for_status()
            # would throw that diagnosis away and leave only "400 Bad
            # Request" in the traceback.
            raise ValueError(
                f"Tavily search failed (HTTP {resp.status_code}) "
                f"for query {query!r}: {resp.text.strip()[:500]}"
            )
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            }
            for r in resp.json().get("results", [])
        ]


class MockBackend:
    """Offline backend for testing the pipeline end-to-end without keys."""

    def search(self, query: str, max_results: int) -> list[dict]:
        return [
            {
                "title": f"[MOCK] Result for: {query}",
                "url": "https://example.com/mock",
                "content": (
                    "This is canned mock content standing in for a real "
                    "search snippet. In live mode this would be a recent "
                    "news excerpt relevant to the query."
                ),
            }
        ]


def build_queries(question: Question) -> list[str]:
    """Fixed-template queries: the Phase 1 approach, kept as the fallback.

    The raw question plus two framing variants that tend to surface
    base-rate and recency information. Used in mock mode and whenever the
    LLM query-generation call fails.
    """
    return [
        question.text,
        f"{question.text} latest news",
        f"{question.text} historical precedent base rate",
    ]


def gather(question: Question, cfg: Config, mock: bool = False) -> str:
    """Run all queries and assemble a research digest string for the prompt."""
    backend: SearchBackend = (
        MockBackend() if mock else TavilyBackend(cfg.tavily_api_key)
    )

    if mock:
        queries = build_queries(question)
    else:
        try:
            queries = generate_queries(question, cfg)
        except Exception as exc:  # noqa: BLE001 — any failure here must not
            # block the forecast; the templates are a serviceable fallback.
            print(
                f"Query generation failed ({exc}); "
                "falling back to template queries.",
                file=sys.stderr,
            )
            queries = build_queries(question)

    # Show the queries so the human can judge retrieval quality — thin or
    # off-target queries are the usual culprit behind a high-spread forecast.
    for q in queries:
        print(f"  query: {q}")

    seen_urls: set[str] = set()
    sections: list[str] = []
    for query in queries:
        results = backend.search(query, cfg.search_results_per_query)
        for r in results:
            if r["url"] in seen_urls:
                continue  # dedupe across overlapping queries
            seen_urls.add(r["url"])
            sections.append(f"SOURCE: {r['title']} ({r['url']})\n{r['content']}")

    if not sections:
        return "No research results were found. Reason from general knowledge only."
    return "\n\n---\n\n".join(sections)
