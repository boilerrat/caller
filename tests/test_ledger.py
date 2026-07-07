"""Tests for the prediction ledger: record, resolve, Brier math, migration.

The ledger is the non-negotiable part of the project — the calibration
record everything else exists to build. These tests pin down the Brier
arithmetic and the schema migration so neither can silently drift.
"""

import sqlite3
from datetime import date

import pytest

from caller.aggregate import aggregate
from caller.ledger import Ledger
from caller.question import Question
from caller.reasoning import ForecastRun


def _forecast(*probs):
    return aggregate(
        [ForecastRun(probability=p, rationale=f"run at {p}") for p in probs]
    )


def _question():
    return Question(
        text="Will X happen?",
        resolution_date=date(2026, 12, 31),
        criteria="Resolves YES if X.",
    )


def test_record_returns_id_and_persists(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    pid = book.record(_question(), _forecast(0.3, 0.4, 0.5))
    rows = book.rows()
    assert pid == 1
    assert len(rows) == 1
    assert rows[0].probability == 0.4
    assert rows[0].outcome is None


def test_record_keeps_rationale_closest_to_median(tmp_path):
    db = str(tmp_path / "test.db")
    book = Ledger(db)
    book.record(_question(), _forecast(0.2, 0.4, 0.9))
    rationale = book.conn.execute(
        "SELECT rationale FROM predictions WHERE id = 1"
    ).fetchone()[0]
    assert rationale == "run at 0.4"


def test_record_stores_raw_question_when_given(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    book.record(_question(), _forecast(0.4), raw_question="will x happen soon??")
    raw = book.conn.execute(
        "SELECT raw_question FROM predictions WHERE id = 1"
    ).fetchone()[0]
    assert raw == "will x happen soon??"


def test_record_raw_question_defaults_to_null(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    book.record(_question(), _forecast(0.4))
    raw = book.conn.execute(
        "SELECT raw_question FROM predictions WHERE id = 1"
    ).fetchone()[0]
    assert raw is None


def test_resolve_computes_brier_for_no(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    pid = book.record(_question(), _forecast(0.37))
    # brier = (0.37 - 0)^2 = 0.1369
    assert book.resolve(pid, outcome_yes=False) == pytest.approx(0.1369)


def test_resolve_computes_brier_for_yes(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    pid = book.record(_question(), _forecast(0.37))
    # brier = (0.37 - 1)^2 = 0.3969
    assert book.resolve(pid, outcome_yes=True) == pytest.approx(0.3969)


def test_resolve_unknown_id_raises(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    with pytest.raises(KeyError):
        book.resolve(999, outcome_yes=True)


def test_calibration_summary_reports_mean_brier(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    p1 = book.record(_question(), _forecast(0.1))
    p2 = book.record(_question(), _forecast(0.9))
    book.resolve(p1, outcome_yes=False)  # brier 0.01
    book.resolve(p2, outcome_yes=True)   # brier 0.01 (rounded 0.0100)
    summary = book.calibration_summary()
    assert "Total predictions: 2" in summary
    assert "Resolved:          2" in summary
    assert "0.0100" in summary


def test_calibration_summary_with_nothing_resolved(tmp_path):
    book = Ledger(str(tmp_path / "test.db"))
    book.record(_question(), _forecast(0.5))
    assert "No resolved predictions yet" in book.calibration_summary()


def test_migration_adds_raw_question_to_old_database(tmp_path):
    """A ledger created before Phase 2 must open and keep working."""
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE predictions (
               id              INTEGER PRIMARY KEY AUTOINCREMENT,
               question        TEXT NOT NULL,
               criteria        TEXT,
               resolution_date TEXT NOT NULL,
               probability     REAL NOT NULL,
               runs            INTEGER NOT NULL,
               spread          REAL NOT NULL,
               rationale       TEXT,
               created_at      TEXT NOT NULL,
               outcome         INTEGER,
               brier           REAL
           )"""
    )
    conn.execute(
        """INSERT INTO predictions
           (question, criteria, resolution_date, probability, runs, spread,
            rationale, created_at)
           VALUES ('old q', '', '2026-01-01', 0.5, 5, 0.1, 'r', '2026-01-01')"""
    )
    conn.commit()
    conn.close()

    book = Ledger(db)  # must run the ALTER TABLE migration
    cols = {r[1] for r in book.conn.execute("PRAGMA table_info(predictions)")}
    assert "raw_question" in cols
    # Old row survives with NULL raw_question; new rows can set it.
    assert book.rows()[0].question == "old q"
    book.record(_question(), _forecast(0.4), raw_question="raw")
    assert len(book.rows()) == 2
