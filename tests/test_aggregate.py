"""Tests for median aggregation and the spread instability signal."""

import pytest

from caller.aggregate import aggregate
from caller.reasoning import ForecastRun


def _runs(*probs) -> list[ForecastRun]:
    return [ForecastRun(probability=p, rationale=f"run at {p}") for p in probs]


def test_median_of_odd_run_count():
    assert aggregate(_runs(0.2, 0.4, 0.9)).probability == 0.4


def test_median_of_even_run_count():
    assert aggregate(_runs(0.2, 0.4)).probability == pytest.approx(0.3)


def test_median_is_robust_to_outlier():
    # The whole point of median over mean: one wild 0.95 run must not drag
    # the forecast (the mean here would be ~0.42).
    forecast = aggregate(_runs(0.30, 0.31, 0.32, 0.33, 0.95))
    assert forecast.probability == 0.32


def test_zero_runs_raises():
    with pytest.raises(ValueError):
        aggregate([])


def test_spread_is_max_minus_min():
    assert aggregate(_runs(0.2, 0.4, 0.9)).spread == pytest.approx(0.7)


def test_summary_warns_on_high_spread():
    assert "high spread" in aggregate(_runs(0.2, 0.5, 0.9)).summary()


def test_summary_quiet_on_low_spread():
    assert "high spread" not in aggregate(_runs(0.40, 0.42, 0.44)).summary()
