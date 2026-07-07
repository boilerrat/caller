"""Question representation.

A forecast is only as good as its question. Superforecasting practice (and
every prediction market) demands three things be pinned down before any
research happens:

  1. The event, stated unambiguously.
  2. The resolution criteria — what evidence settles it YES vs NO.
  3. The resolution date — when we stop waiting.

For the PoC the user supplies these on the command line. Phase 2 can add an
LLM "formalization" pass that takes a fuzzy question and proposes crisp
criteria for the user to approve.
"""

from dataclasses import dataclass
from datetime import date


@dataclass
class Question:
    text: str                 # e.g. "Will the Bank of Canada cut rates below 2% in 2026?"
    resolution_date: date     # date by which the question resolves
    criteria: str = ""        # optional explicit resolution criteria

    def prompt_block(self) -> str:
        """Render the question as a block for inclusion in LLM prompts."""
        lines = [
            f"QUESTION: {self.text}",
            f"RESOLUTION DATE: {self.resolution_date.isoformat()}",
        ]
        if self.criteria:
            lines.append(f"RESOLUTION CRITERIA: {self.criteria}")
        else:
            lines.append(
                "RESOLUTION CRITERIA: Resolve YES if the event described "
                "occurs on or before the resolution date; otherwise NO."
            )
        return "\n".join(lines)
