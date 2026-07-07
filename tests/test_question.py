"""Tests for the Question contract.

The prompt block is the question's single rendering point for every LLM call
(reasoning, formalization, query generation), so its shape is load-bearing:
a missing criteria line would silently weaken every downstream prompt.
"""

from datetime import date

from caller.question import Question


def test_prompt_block_includes_text_and_date():
    q = Question(text="Will X happen?", resolution_date=date(2026, 12, 31))
    block = q.prompt_block()
    assert "QUESTION: Will X happen?" in block
    assert "RESOLUTION DATE: 2026-12-31" in block


def test_prompt_block_uses_explicit_criteria_when_given():
    q = Question(
        text="Will X happen?",
        resolution_date=date(2026, 12, 31),
        criteria="Resolves YES if X is confirmed by source S.",
    )
    block = q.prompt_block()
    assert "RESOLUTION CRITERIA: Resolves YES if X is confirmed by source S." in block


def test_prompt_block_falls_back_to_default_criteria():
    q = Question(text="Will X happen?", resolution_date=date(2026, 12, 31))
    block = q.prompt_block()
    # The default must still pin down a YES/NO rule, not leave criteria absent.
    assert "RESOLUTION CRITERIA:" in block
    assert "otherwise NO" in block
