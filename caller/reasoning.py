"""Reasoning phase: one structured superforecast per call.

This is the heart of the bot. The prompt encodes the superforecaster
discipline that separates calibrated forecasts from vibes:

  1. OUTSIDE VIEW FIRST — identify a reference class and base rate before
     looking at case-specific details. Anchoring on the base rate is the
     single highest-leverage habit in the forecasting literature.
  2. INSIDE VIEW SECOND — adjust from the anchor using the research digest.
  3. DECOMPOSITION — if the event requires several things to all happen,
     estimate each conditional step; conjunctive chains multiply out to
     smaller numbers than intuition suggests.
  4. STEELMAN BOTH SIDES — explicitly argue the strongest YES and NO cases.
  5. COMMIT TO A NUMBER — a precise probability, not a range.

The model is asked for strict JSON so downstream aggregation can parse it.
Temperature stays at the default (1.0) on purpose: run-to-run diversity is
what makes median aggregation work.
"""

import json
from dataclasses import dataclass

from .config import Config
from .question import Question

SYSTEM_PROMPT = """\
You are a superforecaster with an exceptional calibration track record. You
follow the discipline of Tetlock's Good Judgment Project: outside view before
inside view, decomposition of conjunctive events, explicit steelmanning of
both outcomes, and precise probabilistic commitment.

You will receive a forecasting question, its resolution criteria and date,
and a digest of recent research. Work through these steps IN ORDER:

1. Restate the question and what exactly resolves it YES.
2. Identify the best reference class and state a numeric base rate for it.
3. From the research digest, list the strongest evidence pointing YES and
   the strongest evidence pointing NO. Weigh recency and source quality.
4. If the event requires multiple conditions to all hold, decompose it and
   estimate each conditional probability.
5. Adjust from the base rate using the evidence. State the direction and
   rough size of each adjustment.
6. Sanity-check against the time remaining until the resolution date.

Then respond with ONLY a JSON object, no markdown fences, no preamble:
{
  "reference_class": "<the reference class you used>",
  "base_rate": <number 0-1 or null if no sensible base rate exists>,
  "rationale": "<3-6 sentence summary of your reasoning>",
  "probability": <your final probability that the question resolves YES, 0.01-0.99>
}

Never output a probability of exactly 0 or 1 — resolution criteria surprises
and black swans always leave residual uncertainty."""


@dataclass
class ForecastRun:
    probability: float
    rationale: str
    reference_class: str = ""
    base_rate: float | None = None
    raw: str = ""  # full model output, kept for debugging/audit


def _parse(raw: str) -> ForecastRun:
    """Parse the model's JSON output, tolerating stray markdown fences."""
    cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
    # If the model added prose despite instructions, grab the outermost braces.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model output:\n{raw}")
    data = json.loads(cleaned[start : end + 1])

    prob = float(data["probability"])
    # Clamp defensively — the ledger and Brier math assume (0, 1).
    prob = min(max(prob, 0.01), 0.99)

    return ForecastRun(
        probability=prob,
        rationale=str(data.get("rationale", "")),
        reference_class=str(data.get("reference_class", "")),
        base_rate=data.get("base_rate"),
        raw=raw,
    )


def run_once(question: Question, research_digest: str, cfg: Config) -> ForecastRun:
    """Execute a single live reasoning run against the Anthropic API."""
    # Imported lazily so mock mode works without the SDK installed.
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    message = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{question.prompt_block()}\n\n"
                    f"RESEARCH DIGEST:\n{research_digest}"
                ),
            }
        ],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return _parse(raw)


def run_once_mock(question: Question, research_digest: str) -> ForecastRun:
    """Offline stand-in that exercises the same parse path as live runs."""
    import random

    fake_prob = round(random.uniform(0.30, 0.45), 2)
    fake_json = json.dumps(
        {
            "reference_class": "[MOCK] comparable historical events",
            "base_rate": 0.35,
            "rationale": (
                "[MOCK] This rationale is generated offline to test the "
                "pipeline. Live runs produce real reasoning here."
            ),
            "probability": fake_prob,
        }
    )
    return _parse(fake_json)
