"""Tests for the formalization parse path.

The formalization proposal replaces the user's question text downstream, so
an empty or missing field here would silently forecast a blank question —
the parser must fail loudly instead.
"""

import json
from datetime import date

import pytest

from caller.formalize import Formalization, _parse, propose_mock


def _payload(**overrides) -> str:
    data = {
        "question": "Will X exceed 5 by 2026-12-31?",
        "criteria": "Resolves YES if source S reports X > 5; otherwise NO.",
        "resolution_source": "Source S official statistics",
        "ambiguities": ["'X' was undefined; interpreted as the official metric."],
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_valid_proposal():
    prop = _parse(_payload())
    assert isinstance(prop, Formalization)
    assert prop.question == "Will X exceed 5 by 2026-12-31?"
    assert "otherwise NO" in prop.criteria
    assert prop.resolution_source == "Source S official statistics"
    assert len(prop.ambiguities) == 1


def test_parse_strips_markdown_fences():
    prop = _parse(f"```json\n{_payload()}\n```")
    assert prop.question


def test_parse_raises_on_missing_question():
    with pytest.raises(ValueError):
        _parse(_payload(question=""))


def test_parse_raises_on_missing_criteria():
    with pytest.raises(ValueError):
        _parse(_payload(criteria=""))


def test_parse_raises_on_no_json():
    with pytest.raises(ValueError):
        _parse("Sorry, I cannot formalize this.")


def test_parse_defaults_ambiguities_to_empty_list():
    prop = _parse(_payload(ambiguities=None))
    assert prop.ambiguities == []


def test_propose_mock_exercises_parse_path():
    prop = propose_mock("Will it rain?", date(2026, 12, 31))
    assert prop.question
    assert prop.criteria
    assert "2026-12-31" in prop.criteria


def test_display_renders_all_fields():
    prop = _parse(_payload())
    text = prop.display()
    assert "QUESTION:" in text
    assert "CRITERIA:" in text
    assert "SOURCE:" in text
    assert "AMBIGUITIES RESOLVED:" in text


def test_display_notes_when_already_crisp():
    prop = _parse(_payload(ambiguities=[]))
    assert "already crisp" in prop.display()
