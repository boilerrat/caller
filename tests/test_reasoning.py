"""Tests for the reasoning JSON parse path.

_parse() is the trust boundary between the model's free-text output and the
numeric pipeline — every invariant here (clamping, fence tolerance, loud
failure on garbage) protects the ledger from recording a corrupted forecast.
"""

import json

import pytest

from caller.question import Question
from caller.reasoning import ForecastRun, _parse, run_once_mock
from datetime import date


def _payload(prob) -> str:
    return json.dumps(
        {
            "reference_class": "test class",
            "base_rate": 0.4,
            "rationale": "test rationale",
            "probability": prob,
        }
    )


def test_parse_valid_json():
    run = _parse(_payload(0.42))
    assert isinstance(run, ForecastRun)
    assert run.probability == 0.42
    assert run.rationale == "test rationale"
    assert run.reference_class == "test class"
    assert run.base_rate == 0.4


def test_parse_strips_markdown_fences():
    run = _parse(f"```json\n{_payload(0.42)}\n```")
    assert run.probability == 0.42


def test_parse_tolerates_surrounding_prose():
    run = _parse(f"Here is my forecast:\n{_payload(0.42)}\nThank you.")
    assert run.probability == 0.42


def test_parse_clamps_probability_above_ceiling():
    assert _parse(_payload(1.0)).probability == 0.99


def test_parse_clamps_probability_below_floor():
    assert _parse(_payload(0.0)).probability == 0.01


def test_parse_raises_on_no_json():
    with pytest.raises(ValueError):
        _parse("I cannot answer this question.")


def test_parse_raises_on_malformed_json():
    with pytest.raises(ValueError):
        _parse("{probability: not valid json")


def test_parse_raises_on_missing_probability():
    with pytest.raises((KeyError, ValueError)):
        _parse('{"rationale": "no probability field"}')


def test_parse_keeps_raw_output_for_audit():
    raw = _payload(0.42)
    assert _parse(raw).raw == raw


def test_run_once_mock_exercises_parse_path():
    q = Question(text="Will X happen?", resolution_date=date(2026, 12, 31))
    run = run_once_mock(q, "digest")
    # The mock draws from [0.30, 0.45] — inside the clamp range on purpose,
    # so the value must come through the parser unmodified.
    assert 0.30 <= run.probability <= 0.45
    assert run.rationale
