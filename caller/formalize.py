"""Formalization pass: turn a fuzzy question into a resolvable one.

A forecast on an ambiguous question is unstable no matter how good the
reasoning is — the live Phase 1 tests showed exactly this: a well-specified
rate question produced a run spread of 0.02, while "Will AI be a major issue
in the midterms?" produced 0.30, because independent runs disagreed about
what "major issue" even means. The leak was in the question, not the model.

This module is the fix. It makes one focused LLM call that takes the user's
raw question and proposes a formalization:

  * a sharpened question with a measurable threshold,
  * explicit resolution criteria naming what evidence settles YES vs NO,
  * an authoritative source to check it against,
  * and a list of ambiguities found in the original phrasing.

The critical design decision: the output is a PROPOSAL, never an automatic
rewrite. The CLI shows it to the human, who accepts, retries, or aborts
before any research or reasoning spends a token. The bot sharpens the
question; the human still owns it.

Follows the same shape as reasoning.py: strict JSON contract, a `_parse()`
that tolerates stray markdown fences, and a mock path so `--mock` exercises
the full pipeline offline.
"""

import json
from dataclasses import dataclass, field
from datetime import date

from .config import Config

SYSTEM_PROMPT = """\
You are a question formalizer for a forecasting system, in the tradition of
Metaculus moderators and prediction-market resolution councils. Forecasts are
only meaningful when the question is resolvable: an independent third party,
reading only the question and criteria, must be able to look at the world on
the resolution date and say YES or NO with no judgment calls.

You will receive a raw forecasting question and its resolution date. Produce
a formalized version that:

1. States one binary event with a measurable threshold. Replace vague
   predicates ("major", "successful", "popular", "soon") with numbers,
   named events, or verifiable facts.
2. Specifies resolution criteria: exactly what evidence resolves YES, and
   what the default is otherwise.
3. Names an authoritative source to check (an official statistic, a specific
   organization's announcement, a market close on a named exchange).
4. Preserves the user's evident intent — sharpen the question they asked,
   do not substitute a different question they didn't.
5. Lists every ambiguity you found in the original phrasing, so the user
   sees what interpretive choices you made on their behalf.

Then respond with ONLY a JSON object, no markdown fences, no preamble:
{
  "question": "<the formalized binary question>",
  "criteria": "<resolution criteria: what resolves YES, default otherwise>",
  "resolution_source": "<the authoritative source to check on the resolution date>",
  "ambiguities": ["<each ambiguity in the original and how you resolved it>"]
}

If the original question is already crisp, return it near-verbatim with an
empty ambiguities list — do not manufacture changes to look useful."""


@dataclass
class Formalization:
    question: str
    criteria: str
    resolution_source: str = ""
    ambiguities: list[str] = field(default_factory=list)
    raw: str = ""  # full model output, kept for debugging/audit

    def display(self) -> str:
        """Render the proposal for the terminal approval prompt."""
        lines = [
            "Proposed formalization:",
            f"  QUESTION:  {self.question}",
            f"  CRITERIA:  {self.criteria}",
        ]
        if self.resolution_source:
            lines.append(f"  SOURCE:    {self.resolution_source}")
        if self.ambiguities:
            lines.append("  AMBIGUITIES RESOLVED:")
            lines.extend(f"    - {a}" for a in self.ambiguities)
        else:
            lines.append("  (original question was already crisp)")
        return "\n".join(lines)


def _parse(raw: str) -> Formalization:
    """Parse the model's JSON output, tolerating stray markdown fences."""
    cleaned = raw.strip().replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in formalization output:\n{raw}")
    data = json.loads(cleaned[start : end + 1])

    question = str(data.get("question", "")).strip()
    criteria = str(data.get("criteria", "")).strip()
    # A formalization without a question or criteria is useless downstream —
    # fail here rather than let an empty question reach research/reasoning.
    if not question or not criteria:
        raise ValueError(
            f"Formalization output is missing question or criteria:\n{raw}"
        )

    ambiguities = data.get("ambiguities") or []
    return Formalization(
        question=question,
        criteria=criteria,
        resolution_source=str(data.get("resolution_source", "")).strip(),
        ambiguities=[str(a) for a in ambiguities],
        raw=raw,
    )


def propose(question_text: str, resolution_date: date, cfg: Config) -> Formalization:
    """One live formalization call against the Anthropic API.

    Uses the main reasoning model rather than a cheaper one: judging what an
    ambiguous question "really means" is exactly the kind of judgment-heavy
    task where model quality shows, and it's one call per forecast.
    """
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
                    f"RAW QUESTION: {question_text}\n"
                    f"RESOLUTION DATE: {resolution_date.isoformat()}"
                ),
            }
        ],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return _parse(raw)


def propose_mock(question_text: str, resolution_date: date) -> Formalization:
    """Offline stand-in that exercises the same parse path as live calls."""
    fake_json = json.dumps(
        {
            "question": f"[MOCK formalized] {question_text}",
            "criteria": (
                "[MOCK] Resolves YES if the mock event occurs on or before "
                f"{resolution_date.isoformat()}; otherwise NO."
            ),
            "resolution_source": "[MOCK] example.com official mock registry",
            "ambiguities": [
                "[MOCK] 'the event' was undefined; interpreted as the mock event."
            ],
        }
    )
    return _parse(fake_json)
