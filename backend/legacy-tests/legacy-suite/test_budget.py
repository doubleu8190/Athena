"""预算控制测试."""

from __future__ import annotations

import pytest

from athena.core.harness.budget import Budget, BudgetExceeded


def test_budget_increment_turn():
    b = Budget(max_turns=3, retry_budget=2)
    b.increment_turn()
    b.increment_turn()
    assert b.turn_count == 2
    assert b.remaining_turns() == 1


def test_budget_exceeds_turns():
    b = Budget(max_turns=2, retry_budget=5)
    b.increment_turn()
    b.increment_turn()
    with pytest.raises(BudgetExceeded):
        b.increment_turn()


def test_budget_exceeds_retries():
    b = Budget(max_turns=10, retry_budget=1)
    b.increment_retry()
    with pytest.raises(BudgetExceeded):
        b.increment_retry()


def test_budget_reset_retries():
    b = Budget(max_turns=10, retry_budget=2)
    b.increment_retry()
    b.increment_retry()
    b.reset_retries()
    assert b.retry_count == 0
    assert b.remaining_retries() == 2


def test_budget_is_exceeded():
    b = Budget(max_turns=1, retry_budget=1)
    b.increment_turn()
    with pytest.raises(BudgetExceeded):
        b.increment_turn()
    assert b.is_exceeded()
