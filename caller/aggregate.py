"""Aggregation phase: many runs in, one forecast out.

Why aggregate at all? A single LLM reasoning pass is noisy — the same model
on the same evidence lands on noticeably different numbers run to run. That
noise is roughly symmetric, so the median of N independent runs cancels most
of it and consistently outperforms any individual run (the same logic as
crowd aggregation among human forecasters, just with a crowd of one model).

Median is preferred over mean because it's robust to the occasional wild
outlier run (a misparse-adjacent 0.05 or 0.95 shouldn't drag the forecast).
"""

import statistics
from dataclasses import dataclass, field

from .reasoning import ForecastRun


@dataclass
class AggregatedForecast:
    probability: float               # the headline number: median of runs
    runs: list[ForecastRun] = field(default_factory=list)

    @property
    def spread(self) -> float:
        """Max - min across runs. Large spread flags an unstable forecast —
        usually a sign the question is ambiguous or the research was thin."""
        probs = [r.probability for r in self.runs]
        return max(probs) - min(probs) if probs else 0.0

    def summary(self) -> str:
        probs = ", ".join(f"{r.probability:.2f}" for r in self.runs)
        return (
            f"Median probability: {self.probability:.2f}\n"
            f"Individual runs:    [{probs}]\n"
            f"Spread:             {self.spread:.2f}"
            + ("  ⚠ high spread — treat with caution" if self.spread > 0.25 else "")
        )


def aggregate(runs: list[ForecastRun]) -> AggregatedForecast:
    if not runs:
        raise ValueError("Cannot aggregate zero runs.")
    median = statistics.median(r.probability for r in runs)
    return AggregatedForecast(probability=round(median, 3), runs=runs)
