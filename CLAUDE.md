# caller — project context for Claude Code

`caller` is a Phase 1 proof-of-concept AI superforecasting bot. It implements
the retrieval → reasoning → aggregation architecture from Halawi et al.
(2024), the same pattern used by Metaculus tournament bots and commercial AI
forecasters: research (Tavily, pluggable backend) → structured superforecaster
reasoning (Anthropic API, `claude-sonnet-4-6`, strict JSON output) → median
aggregation over N independent runs → SQLite ledger with Brier scoring.

Phase 1 and Phase 2 are complete and verified both in mock mode and with
live runs (the user's Anthropic + Tavily keys are in `.env`, loaded via
`set -a; source .env; set +a`; a venv lives at `venv/`). The ledger
(`caller.db`) holds real predictions. Live validation evidence worth
knowing: an ambiguous question produced a run spread of 0.30; the same
question through the `--formalize` pass tightened to 0.02.

## Architecture

Five small, independently replaceable modules, wired together by `cli.py`:

| Module | Responsibility |
|--------|----------------|
| `caller/question.py` | `Question` dataclass (text, resolution date, criteria) + `prompt_block()` to render it for the LLM. |
| `caller/formalize.py` | Phase 2 formalization pass. One LLM call proposing: sharpened question, explicit YES/NO criteria, named resolution source, and a disclosed list of ambiguities resolved. Always a proposal, never an automatic rewrite — the CLI (`--formalize` flag, or standalone `formalize` subcommand) shows it at an `[a]ccept / [r]etry / [q]uit` prompt before research runs. Same strict-JSON + fence-tolerant `_parse()` shape as reasoning.py; fails loudly on empty question/criteria. `propose_mock()` keeps `--mock` key-free. |
| `caller/research.py` | `SearchBackend` protocol; `TavilyBackend` (live) and `MockBackend` (offline). Live mode: `generate_queries()` decomposes the question into 4–6 targeted queries via one cheap LLM call (`CALLER_QUERY_MODEL`, default `claude-haiku-4-5`) covering base rate, key actors, recent developments, status-quo value; **today's date is injected into that call** because the model otherwise guesses a stale year from training data. Fixed 3-template `build_queries()` is the fallback (mock mode, or any query-generation failure — degrade gracefully, never block a forecast). Dedupes results by URL, assembles a plain-text research digest, prints the queries used so the human can judge retrieval quality. Swapping in Exa/Brave/AskNews means implementing one `search()` method. Tavily HTTP errors surface the response body (a `ValueError` routed through cli.py's handler). |
| `caller/reasoning.py` | One structured superforecast per call. System prompt enforces the Tetlock/Good Judgment Project discipline in order: outside view (reference class + base rate) → inside view (weigh evidence) → decomposition of conjunctive events → steelman both YES and NO → commit to a precise probability. Demands strict JSON output (no markdown fences); `_parse()` tolerates stray fences, raises `ValueError` naming the offending run on malformed JSON, and clamps probability to [0.01, 0.99]. Temperature is left at default (1.0) *on purpose* — run-to-run diversity is what makes median aggregation work. `run_once_mock()` exercises the same parse path offline with a random probability in [0.30, 0.45]. |
| `caller/aggregate.py` | `aggregate()` takes N `ForecastRun`s → median probability (robust to outlier runs, unlike mean). `AggregatedForecast.spread` = max−min across runs; >0.25 triggers a "⚠ high spread" caution in the summary — usually means the question is ambiguous or the research digest was thin. |
| `caller/ledger.py` | SQLite store (`predictions` table). `record()` logs a forecast, keeping the rationale from whichever run's probability sits closest to the median as the "representative" rationale; optional `raw_question` records the user's original phrasing when formalization rewrote it (nullable column, auto-migrated onto pre-Phase-2 databases via a `PRAGMA table_info` check + `ALTER TABLE` in `__init__`). `resolve()` marks an outcome and computes Brier score `(probability - outcome)^2`. `calibration_summary()` reports mean Brier across resolved questions. This ledger is the non-negotiable part of the project — no capital decision should depend on the bot's output until it has a resolved track record. |
| `caller/metaculus.py` | Phase 3. Thin client over three Metaculus API endpoints, ported from the official metac-bot-template (deliberately NOT the `forecasting-tools` framework, which would replace our pipeline): `GET /api/posts/` (list, filtered by tournament/open/binary), `POST /api/questions/forecast/` (payload is a JSON **array**; binary = `probability_yes`), `POST /api/comments/create/` (private rationale comment). Auth: `Authorization: Token <METACULUS_TOKEN>` (bot account from metaculus.com/aib). **Critical API gotcha, verified live: the /posts/ list response returns `my_forecasts: null` regardless of forecast state — only `GET /api/posts/{id}/` (detail) populates it.** So already-forecasted dedup goes through `client.already_forecasted(post_id)` (one detail GET), which the CLI calls lazily until the sweep `--limit` is filled. `MetaculusQuestion.to_question()` maps title/criteria/fine-print/background (truncated to 1500 chars) + `scheduled_resolve_time` into our `Question`; no formalization needed since Metaculus questions arrive pre-formalized. Tournament ids: `bot-testing-area` (sandbox), `minibench`, `33022` (Summer 2026 AI Benchmarking). |
| `caller/config.py` | All runtime config from environment variables (`ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `METACULUS_TOKEN`, `CALLER_MODEL`, `CALLER_QUERY_MODEL`, `CALLER_RUNS`, `CALLER_MAX_TOKENS`, `CALLER_SEARCH_RESULTS`, `CALLER_DB`) so the same code runs unchanged on a laptop, in Docker, or CI. |
| `caller/cli.py` | argparse wiring for `forecast` (with `--formalize` and `--mock` flags), `formalize`, `metaculus` (with `--tournament`, `--limit`, `--dry-run`, `--mock`; mock forces dry-run so mock forecasts can never be submitted; per-question failures don't abort the sweep; dry-run has zero side effects — no submit, no ledger row), `resolve`, `log`, `score` subcommands. `_make_client()` is the test seam for stubbing the Metaculus client. |

## Key design decisions and invariants

- **Probabilities are always clamped to [0.01, 0.99]** — both the prompt and `_parse()` forbid exact 0 or 1, since resolution-criteria surprises and black swans always leave residual uncertainty.
- **Malformed JSON from the model stops the pipeline** rather than logging a corrupted forecast — a `ValueError` is raised identifying which run failed.
- **Live mode without required keys fails fast** with a message naming the missing env var (see `TavilyBackend.__init__` and how `reasoning.run_once` imports `anthropic` lazily so mock mode needs no SDK).
- **Median over mean** for aggregation — robust to a misparse-adjacent 0.05/0.95 outlier dragging the forecast.
- **Brier score interpretation**: 0.25 = coin-flipping; 0.10–0.15 sustained on a real question mix = decent human-forecaster territory. This is the bar before any capital decision depends on this bot.
- Mock mode (`--mock`) requires no API keys and exercises the full pipeline including the JSON parse path, so it's a meaningful smoke test, not just a stub.

## Testing

`tests/` holds 74 pytest tests (the uncovered lines are the live API call
bodies and `__main__.py`). Run with `venv/bin/pytest`; config in
`pytest.ini` (`pythonpath = .` until a pyproject.toml exists). Everything
deterministic is covered: all `_parse()` paths, aggregation, ledger/Brier
math, both schema migrations, query fallback, the CLI lifecycle in mock
mode (including the formalization approval loop via monkeypatched `input`),
and the Metaculus client/sweep (canned JSON shaped like the real API,
submission payload shape pinned as a JSON array, dry-run zero-side-effects
guarantee). Dev deps in `requirements-dev.txt`.

## Roadmap (do not start ahead of explicit approval)

1. ~~**Phase 2 — better research and formalization.**~~ COMPLETE, live-validated at both gates (formalization: spread 0.30 → 0.02 on the same fuzzy question; query generation: digest 6.6k → 21k+ chars with base-rate/status-quo coverage on the same rate question).
2. ~~**Phase 3 — Metaculus integration.**~~ COMPLETE except deployment, live-validated in the bot-testing-area sandbox (submission + rationale comment accepted; re-sweep correctly skips). **Dockerization deliberately deferred by the user** — it runs locally, scheduled via cron (example line in README); don't propose containerizing unless asked.
3. **Phase 4 — publication and markets.** Publish forecasts (Farcaster frame, newsletter section) and, only after a demonstrated calibration record, evaluate market execution given jurisdictional constraints.

## Working conventions (user preference — apply to all work in this repo)

- Modular design with small, specialized, independently replaceable components (already the shape of the four pipeline modules — preserve this when extending).
- Comprehensive comments explaining *why*, not just what (see the docstring-style module headers already in place — match that voice).
- Thorough docs in flowing paragraphs, not just bullet lists.
- Step-by-step work with validation gates: propose a plan and wait for approval before implementing each phase. Do not jump ahead to Phase 2/3/4 without explicit sign-off, even if the next step seems obvious.
- Testing happens after core development, not as TDD gating for this project's style (differs from this user's default global TDD-first workflow rule — for `caller`, defer to this project-specific convention).
- Once this becomes a git repo, use an issue-first workflow.

## Not yet true (don't assume)

- No git repository yet — this is a plain directory, not `git init`'d (a `.gitignore` already exists, covering `venv/`, `.env`, `caller.db`, caches). When it becomes one, switch to the issue-first workflow.
- No Docker or deployment configuration — deliberately (user's call), it runs locally.
- No cron job is actually installed yet — only documented in the README.
- Ledger row #14 is the sandbox (bot-testing-area) test submission — a real question resolving 2027-01-31, kept unless the user wants it removed. Mock rows were cleaned 2026-07-06.
