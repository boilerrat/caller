"""Prediction ledger: the calibration record.

Every forecast is logged the moment it's made; every resolution is logged
when reality answers. The Brier score — mean squared error between stated
probability and the 0/1 outcome — is the standard calibration metric:

    brier = (probability - outcome)^2      # per question
    lower is better; 0.25 = coin-flipping, ~0.10-0.15 = decent human
    forecaster territory on typical question mixes.

This ledger is the non-negotiable part of the project. Before any real
capital touches a market, this table needs enough resolved questions to
show the bot is actually calibrated — not just confident.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from .aggregate import AggregatedForecast
from .question import Question

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question        TEXT NOT NULL,
    raw_question    TEXT,              -- user's original phrasing, when a
                                       -- formalization pass rewrote it
    criteria        TEXT,
    resolution_date TEXT NOT NULL,     -- ISO date
    probability     REAL NOT NULL,     -- aggregated (median) probability of YES
    runs            INTEGER NOT NULL,  -- number of reasoning runs aggregated
    spread          REAL NOT NULL,     -- max-min across runs (stability signal)
    rationale       TEXT,              -- rationale from the run closest to median
    created_at      TEXT NOT NULL,     -- ISO timestamp
    outcome         INTEGER,           -- NULL until resolved; then 1=YES, 0=NO
    brier           REAL               -- NULL until resolved
);
"""


@dataclass
class LedgerRow:
    id: int
    question: str
    resolution_date: str
    probability: float
    outcome: int | None
    brier: float | None


class Ledger:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(SCHEMA)
        # Lightweight migration: databases created before the formalization
        # pass (Phase 2) lack raw_question. ALTER TABLE is safe to gate on a
        # column check and keeps old ledgers working without manual surgery.
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(predictions)")
        }
        if "raw_question" not in cols:
            self.conn.execute(
                "ALTER TABLE predictions ADD COLUMN raw_question TEXT"
            )
        self.conn.commit()

    # --- writes ---------------------------------------------------------

    def record(
        self,
        q: Question,
        forecast: AggregatedForecast,
        raw_question: str | None = None,
    ) -> int:
        """Log a fresh forecast; returns the prediction id.

        `raw_question` is the user's original phrasing when a formalization
        pass rewrote it — kept so the ledger always shows both what was asked
        and what was actually forecast. None when no rewrite happened.
        """
        # Keep the rationale from the run whose probability sits closest to
        # the median — it best represents the aggregate's "reasoning".
        rep = min(
            forecast.runs,
            key=lambda r: abs(r.probability - forecast.probability),
        )
        cur = self.conn.execute(
            """INSERT INTO predictions
               (question, raw_question, criteria, resolution_date,
                probability, runs, spread, rationale, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                q.text,
                raw_question,
                q.criteria,
                q.resolution_date.isoformat(),
                forecast.probability,
                len(forecast.runs),
                forecast.spread,
                rep.rationale,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def resolve(self, prediction_id: int, outcome_yes: bool) -> float:
        """Mark a prediction resolved and compute its Brier score."""
        row = self.conn.execute(
            "SELECT probability FROM predictions WHERE id = ?", (prediction_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No prediction with id {prediction_id}")
        probability = row[0]
        outcome = 1 if outcome_yes else 0
        brier = round((probability - outcome) ** 2, 4)
        self.conn.execute(
            "UPDATE predictions SET outcome = ?, brier = ? WHERE id = ?",
            (outcome, brier, prediction_id),
        )
        self.conn.commit()
        return brier

    # --- reads ----------------------------------------------------------

    def rows(self) -> list[LedgerRow]:
        cur = self.conn.execute(
            """SELECT id, question, resolution_date, probability, outcome, brier
               FROM predictions ORDER BY id"""
        )
        return [LedgerRow(*r) for r in cur.fetchall()]

    def calibration_summary(self) -> str:
        rows = self.rows()
        resolved = [r for r in rows if r.brier is not None]
        lines = [
            f"Total predictions: {len(rows)}",
            f"Resolved:          {len(resolved)}",
        ]
        if resolved:
            mean_brier = sum(r.brier for r in resolved) / len(resolved)
            lines.append(f"Mean Brier score:  {mean_brier:.4f}")
            lines.append("(0.25 = coin flip; lower is better)")
        else:
            lines.append("No resolved predictions yet — no Brier score available.")
        return "\n".join(lines)
