"""Central configuration for caller.

All runtime configuration comes from environment variables so the same code
runs unchanged on a laptop, in Docker, or in CI. Copy `.env.example` to
`.env` and export it (`set -a; source .env; set +a`) or use python-dotenv.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # --- LLM settings -------------------------------------------------------
    # Anthropic API key. Required for live forecasts; ignored in --mock mode.
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    # Model used for reasoning. Sonnet is the cost/quality sweet spot for
    # multi-run aggregation; you can point this at any current model string.
    # See https://docs.claude.com/en/api/overview for available models.
    model: str = field(
        default_factory=lambda: os.environ.get("CALLER_MODEL", "claude-sonnet-4-6")
    )
    max_tokens: int = int(os.environ.get("CALLER_MAX_TOKENS", "2000"))

    # --- Research settings --------------------------------------------------
    # Tavily is the default search backend (simple REST API, generous free
    # tier). The research module is deliberately pluggable — see research.py.
    tavily_api_key: str = field(
        default_factory=lambda: os.environ.get("TAVILY_API_KEY", "")
    )
    search_results_per_query: int = int(os.environ.get("CALLER_SEARCH_RESULTS", "5"))
    # Model for the search-query decomposition pass (Phase 2). This is a small
    # structured task, so it defaults to the cheap/fast Haiku tier rather than
    # the main reasoning model — one call per forecast, a few hundred tokens.
    query_model: str = field(
        default_factory=lambda: os.environ.get("CALLER_QUERY_MODEL", "claude-haiku-4-5")
    )

    # --- Metaculus settings ---------------------------------------------------
    # Bot-account API token from https://www.metaculus.com/aib/. Required only
    # for the `metaculus` subcommand; everything else runs without it.
    metaculus_token: str = field(
        default_factory=lambda: os.environ.get("METACULUS_TOKEN", "")
    )

    # --- Aggregation settings -----------------------------------------------
    # Number of independent reasoning runs per forecast. The literature
    # (Halawi et al. 2024) shows median-of-N reliably beats single runs;
    # 5 is a reasonable PoC default, tournament bots often use 10+.
    default_runs: int = int(os.environ.get("CALLER_RUNS", "5"))

    # --- Storage --------------------------------------------------------------
    # SQLite prediction ledger. Lives next to wherever you run the CLI unless
    # overridden — in Docker, mount a volume and point this at it.
    db_path: str = field(
        default_factory=lambda: os.environ.get("CALLER_DB", "caller.db")
    )


def load() -> Config:
    """Return a Config built from the current environment."""
    return Config()
