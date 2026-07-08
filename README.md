# caller

> An AI superforecasting bot: research, structured reasoning, multi-run aggregation, and a calibration ledger in one CLI.

`caller` takes a binary forecasting question, gathers current evidence via web search, runs it through a superforecaster-style reasoning prompt (Anthropic API) several independent times, and logs the median probability to a SQLite ledger. When questions resolve, it computes Brier scores so the bot's calibration is measured — not assumed. It implements the retrieval → reasoning → aggregation architecture from Halawi et al. (2024), the same pattern behind Metaculus tournament bots and commercial AI forecasters.

## Requirements

- Python 3.11+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- A Tavily API key for research ([tavily.com](https://tavily.com), free tier available)

Mock mode (`--mock`) runs the full pipeline offline with no keys.

## Installation

```bash
git clone [https://github.com/boilerrat/caller](https://github.com/boilerrat/caller)
cd caller
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys
set -a; source .env; set +a
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes (live mode) | — | Anthropic API key for the reasoning model |
| `TAVILY_API_KEY` | Yes (live mode) | — | Tavily key for the research phase |
| `METACULUS_TOKEN` | For `metaculus` only | — | Bot-account token from [metaculus.com/aib](https://www.metaculus.com/aib/) |
| `CALLER_MODEL` | No | `claude-sonnet-4-6` | Model used for reasoning and formalization |
| `CALLER_QUERY_MODEL` | No | `claude-haiku-4-5` | Cheap model for search-query generation |
| `CALLER_RUNS` | No | `5` | Reasoning runs aggregated per forecast |
| `CALLER_MAX_TOKENS` | No | `2000` | Max tokens per reasoning run |
| `CALLER_SEARCH_RESULTS` | No | `5` | Search results retrieved per query |
| `CALLER_DB` | No | `caller.db` | Path to the SQLite prediction ledger |

## Usage

Verify the install offline first:

```bash
python -m caller forecast "Will it rain in Toronto tomorrow?" --date 2026-07-07 --mock
```

Make a real forecast:

```bash
python -m caller forecast \
  "Will the Bank of Canada policy rate be below 2.00% on Dec 31 2026?" \
  --date 2026-12-31 \
  --criteria "Resolves YES if the BoC target overnight rate is below 2.00% at year end." \
  --runs 5
```

For a fuzzy question, let the bot propose crisp resolution criteria first.
`--formalize` runs an LLM pass that sharpens the question into a measurable,
independently checkable claim, shows you exactly which interpretive choices it
made, and waits for your approval before any research spends a token. This
matters: in live testing, an ambiguous question ("Will AI be a major issue in
the midterms?") produced runs disagreeing by 0.30, while its formalized
version tightened to 0.02 — the leak was in the question, not the model.

```bash
python -m caller forecast "Will AI be a major issue in the 2026 midterms?" \
  --date 2026-11-15 --formalize --runs 5

# Or preview a formalization without forecasting:
python -m caller formalize "Will AI be a major issue in the 2026 midterms?" \
  --date 2026-11-15
```

When a formalization rewrites your question, the ledger stores both the
original phrasing and the formalized version that was actually forecast.

Record outcomes as questions resolve, and review calibration:

```bash
python -m caller resolve 1 --outcome no   # logs the outcome, computes Brier
python -m caller log                       # list all predictions
python -m caller score                     # mean Brier across resolved questions
```

A Brier score of 0.25 is coin-flipping; sustained scores in the 0.10–0.15 range on a real question mix indicate genuine forecasting skill. Build a resolved track record here before any capital decision depends on this bot's output.

## Metaculus tournaments

The `metaculus` subcommand runs the full pipeline against open binary
questions in a tournament and submits the median forecast plus a private
rationale comment through the Metaculus API. Questions the bot has already
forecasted are skipped, every submission is recorded in the local ledger
with a link back to the Metaculus question, and one failed question never
aborts the rest of a sweep. Metaculus questions arrive with resolution
criteria already pinned down, so no formalization pass is needed on this
path.

```bash
# Sandbox first — validate end-to-end without touching a live tournament:
python -m caller metaculus --tournament bot-testing-area --dry-run --limit 1

# --dry-run runs research + reasoning but submits and records nothing.
# Drop it to submit for real; then point at a live tournament:
python -m caller metaculus --tournament minibench --limit 5
```

`--limit` (default 5) caps spend — each question costs one query-generation
call, ~6 searches, and N reasoning runs. `--mock` exercises the plumbing
offline and always implies `--dry-run`: a mock forecast can never reach a
live tournament.

When Metaculus resolves the questions, `sync` pulls the outcomes and
Brier-scores them — the calibration loop closes without any manual
`resolve` calls. Annulled and ambiguous resolutions are reported but never
scored, since a voided question carries no calibration signal.

```bash
python -m caller sync
```

To forecast and score on a schedule, crontab entries are all it takes (no
containerization needed while running on one machine):

```cron
# Sweep MiniBench four times a day, and score resolutions daily:
0 */6 * * * cd /path/to/caller && set -a; . ./.env; set +a; venv/bin/python -m caller metaculus --tournament minibench --limit 5 >> metaculus_cron.log 2>&1
10 5 * * * cd /path/to/caller && set -a; . ./.env; set +a; venv/bin/python -m caller sync >> metaculus_cron.log 2>&1
```

The client throttles its requests and backs off on HTTP 429 — Metaculus
rate-limits rapid-fire API traffic, and a patient bot is a welcome bot.

## Architecture

The pipeline is five small modules, each replaceable on its own:

| Module | Responsibility |
|--------|----------------|
| `formalize.py` | Optional LLM pass that turns a fuzzy question into a measurable one: sharpened text, explicit YES/NO criteria, a named authoritative source, and a disclosed list of every interpretive choice made. Always a proposal — the human approves, retries, or aborts before anything runs. |
| `research.py` | Decomposes the question into 4–6 targeted search queries with one cheap LLM call (base rates, key actors, recent developments, status-quo values), runs them against a pluggable backend (`TavilyBackend`, `MockBackend`), and assembles a research digest. Falls back to fixed template queries if query generation fails — retrieval degrades gracefully, never blocks a forecast. Swap in Exa, Brave, or AskNews by implementing one method. |
| `reasoning.py` | One structured superforecast per call: outside view (base rate) first, evidence weighing, decomposition, steelmanning, then a committed probability returned as JSON. |
| `aggregate.py` | Median of N independent runs. Reports spread as an instability warning. |
| `ledger.py` | SQLite record of every forecast and resolution, with per-question and mean Brier scoring. Stores the original question phrasing alongside the formalized version when they differ, and the Metaculus question id/URL for submitted tournament forecasts. |
| `metaculus.py` | Thin client over three Metaculus API endpoints (list tournament posts, submit forecast, comment), ported from the official metac-bot-template. Deliberately not the full `forecasting-tools` framework — their endpoints, our pipeline. |

`question.py` defines the question contract (text, resolution criteria, resolution date), `config.py` centralizes environment configuration, and `cli.py` wires the pipeline together.

## Errors and edge cases

- Live mode without keys fails fast with a message naming the missing variable.
- Model output that is not valid JSON raises a parse error identifying the offending run; the pipeline stops rather than logging a corrupted forecast.
- Probabilities are clamped to [0.01, 0.99] — the prompt forbids 0 and 1, and the parser enforces it.
- A run spread above 0.25 prints a caution flag: wide disagreement between runs usually means the question is ambiguous or the research digest was thin.

## Testing

The test suite covers every deterministic path — JSON parsing (with fence
tolerance and clamping), aggregation math, ledger recording and Brier
scoring, schema migration, query building and fallback, and the full CLI
lifecycle in mock mode. No API keys or network access needed.

```bash
pip install -r requirements-dev.txt
pytest                # 74 tests
pytest --cov=caller   # with coverage (the gaps are the live API call bodies)
```

## Roadmap

1. ~~**Phase 2 — better research and formalization.**~~ Done: LLM-generated sub-queries with template fallback, and the `--formalize` approval-gated question pass.
2. ~~**Phase 3 — Metaculus integration.**~~ Done: the `metaculus` subcommand lists open tournament questions, forecasts, submits with rationale comments, and runs fine from cron. Containerization deliberately deferred — it runs locally.
3. **Phase 4 — publication and markets.** Publish forecasts (Farcaster frame, newsletter section) and, only after a demonstrated calibration record, evaluate market execution given jurisdictional constraints.

## License

MIT.
