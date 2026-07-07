"""Tests for the research phase: query building, dedup, and fallback.

The live Tavily and Anthropic calls are not exercised here — what's tested
is everything deterministic around them: the template queries, URL dedup,
the digest shape, the error surfacing on HTTP failures, and the guarantee
that a query-generation failure degrades to templates instead of blocking
the forecast.
"""

from datetime import date

import pytest

from caller import research
from caller.config import Config
from caller.question import Question
from caller.research import MockBackend, TavilyBackend, build_queries, gather


def _question():
    return Question(text="Will X happen?", resolution_date=date(2026, 12, 31))


def _config(**overrides) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_build_queries_covers_recency_and_base_rate():
    queries = build_queries(_question())
    assert queries[0] == "Will X happen?"
    assert any("latest news" in q for q in queries)
    assert any("base rate" in q for q in queries)


def test_tavily_backend_requires_api_key():
    with pytest.raises(ValueError, match="TAVILY_API_KEY"):
        TavilyBackend("")


def test_tavily_backend_surfaces_error_body(monkeypatch):
    """An HTTP failure must carry Tavily's diagnosis, not a bare status."""

    class FakeResponse:
        ok = False
        status_code = 400
        text = '{"detail":{"error":"Query is invalid."}}'

    monkeypatch.setattr(
        research.requests, "post", lambda *a, **k: FakeResponse()
    )
    backend = TavilyBackend("tvly-fake-key")
    with pytest.raises(ValueError, match="Query is invalid"):
        backend.search("...", 5)


def test_gather_mock_dedupes_by_url(capsys):
    # MockBackend returns the same URL for every query, so after dedup the
    # digest must contain exactly one SOURCE section.
    digest = gather(_question(), _config(), mock=True)
    assert digest.count("SOURCE:") == 1


def test_gather_mock_uses_template_queries(capsys):
    gather(_question(), _config(), mock=True)
    out = capsys.readouterr().out
    assert "query: Will X happen?" in out
    assert "latest news" in out


def test_gather_falls_back_to_templates_on_query_generation_failure(
    monkeypatch, capsys
):
    """The LLM query pass failing must never block a forecast."""

    def boom(question, cfg):
        raise RuntimeError("simulated API outage")

    class StubBackend:
        def search(self, query, max_results):
            return [
                {"title": f"result for {query}", "url": f"https://x/{query}",
                 "content": "snippet"}
            ]

    monkeypatch.setattr(research, "generate_queries", boom)
    monkeypatch.setattr(research, "TavilyBackend", lambda key: StubBackend())

    digest = gather(_question(), _config(tavily_api_key="k"), mock=False)
    captured = capsys.readouterr()
    assert "falling back to template queries" in captured.err
    # All 3 template queries ran against the (stub) backend.
    assert digest.count("SOURCE:") == 3


def test_gather_returns_placeholder_when_no_results(monkeypatch):
    class EmptyBackend:
        def search(self, query, max_results):
            return []

    monkeypatch.setattr(research, "TavilyBackend", lambda key: EmptyBackend())
    monkeypatch.setattr(
        research, "generate_queries", lambda q, c: ["only query"]
    )
    digest = gather(_question(), _config(tavily_api_key="k"), mock=False)
    assert "No research results" in digest
