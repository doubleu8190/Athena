"""Tests for routing functions (conditional edge logic)."""


from athena.core.graph.routing import (
    after_plan,
    after_execute,
    after_failure,
    after_collect,
    _build_sends,
    MAX_RETRIES,
    CIRCUIT_BREAKER_THRESHOLD,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_plan(subtasks: list[dict]) -> dict:
    return {"task_id": "task-1", "subtasks": subtasks}


def _make_subtask(step: int, depends_on: list[int] | None = None, **kw) -> dict:
    s = {
        "step": step,
        "intent": f"Do step {step}",
        "tool_name": "file_read",
        "args": {"path": f"/tmp/{step}"},
        "depends_on": depends_on or [],
        "critical": True,
        "on_failure": "abort",
        "fallback_tool": None,
    }
    s.update(kw)
    return s


# ── _build_sends ─────────────────────────────────────────────────────────

class TestBuildSends:
    def test_no_plan(self):
        assert _build_sends({"task_plan": None}) == []

    def test_all_done(self):
        plan = _make_plan([_make_subtask(1)])
        state = {"task_plan": plan, "completed_steps": [1], "failed_steps": []}
        assert _build_sends(state) == []

    def test_single_ready(self):
        plan = _make_plan([_make_subtask(1)])
        state = {"task_plan": plan, "completed_steps": [], "failed_steps": []}
        sends = _build_sends(state)
        assert len(sends) == 1
        assert sends[0].node == "execute"
        assert sends[0].arg["subtask"]["step"] == 1

    def test_waiting_for_dependency(self):
        plan = _make_plan([
            _make_subtask(1),
            _make_subtask(2, depends_on=[1]),
        ])
        state = {"task_plan": plan, "completed_steps": [], "failed_steps": []}
        sends = _build_sends(state)
        assert len(sends) == 1
        assert sends[0].arg["subtask"]["step"] == 1

    def test_dependency_satisfied(self):
        plan = _make_plan([
            _make_subtask(1),
            _make_subtask(2, depends_on=[1]),
        ])
        state = {"task_plan": plan, "completed_steps": [1], "failed_steps": []}
        sends = _build_sends(state)
        assert len(sends) == 1
        assert sends[0].arg["subtask"]["step"] == 2

    def test_parallel_independent(self):
        plan = _make_plan([
            _make_subtask(1),
            _make_subtask(2),
            _make_subtask(3),
        ])
        state = {"task_plan": plan, "completed_steps": [], "failed_steps": []}
        sends = _build_sends(state)
        assert len(sends) == 3

    def test_skips_failed(self):
        plan = _make_plan([_make_subtask(1), _make_subtask(2)])
        state = {"task_plan": plan, "completed_steps": [], "failed_steps": [1]}
        sends = _build_sends(state)
        # Step 1 failed, step 2 depends on nothing → ready
        assert len(sends) == 1
        assert sends[0].arg["subtask"]["step"] == 2

    def test_diamond_dependency(self):
        """Step 3 depends on both step 1 and 2 completing."""
        plan = _make_plan([
            _make_subtask(1),
            _make_subtask(2),
            _make_subtask(3, depends_on=[1, 2]),
        ])
        # Only step 1 done → step 2 ready, step 3 not
        state = {"task_plan": plan, "completed_steps": [1], "failed_steps": []}
        sends = _build_sends(state)
        assert len(sends) == 1
        assert sends[0].arg["subtask"]["step"] == 2


# ── after_plan ───────────────────────────────────────────────────────────

class TestAfterPlan:
    def test_plan_error(self):
        assert after_plan({"plan_error": "something wrong"}) == "__end__"

    def test_empty_plan(self):
        assert after_plan({"task_plan": None, "plan_error": None}) == "__end__"

    def test_no_subtasks(self):
        assert after_plan({
            "task_plan": {"subtasks": []},
            "plan_error": None,
            "completed_steps": [],
            "failed_steps": [],
        }) == "__end__"

    def test_ready_subtasks(self):
        state = {
            "task_plan": _make_plan([_make_subtask(1)]),
            "plan_error": None,
            "completed_steps": [],
            "failed_steps": [],
        }
        result = after_plan(state)
        assert isinstance(result, list)
        assert len(result) == 1


# ── after_execute ────────────────────────────────────────────────────────

class TestAfterExecute:
    def test_success(self):
        assert after_execute({"subtask_result": {"status": "success"}}) == "collect"

    def test_skipped(self):
        assert after_execute({"subtask_result": {"status": "skipped"}}) == "collect"

    def test_user_rejected(self):
        assert after_execute({"subtask_result": {"status": "user_rejected"}}) == "collect"

    def test_blocked(self):
        assert after_execute({"subtask_result": {"status": "blocked"}}) == "handle_failure"

    def test_failed_with_retries_left(self):
        assert after_execute({
            "subtask_result": {"status": "failed", "step": 1},
            "retry_counts": {1: 0},
        }) == "retry"

    def test_failed_retries_exhausted(self):
        assert after_execute({
            "subtask_result": {"status": "failed", "step": 1},
            "retry_counts": {1: MAX_RETRIES},
        }) == "handle_failure"


# ── after_failure ────────────────────────────────────────────────────────

class TestAfterFailure:
    def test_fallback(self):
        assert after_failure({"subtask_result": {"status": "fallback"}}) == "execute"

    def test_abort(self):
        assert after_failure({"subtask_result": {"status": "abort"}}) == "collect"

    def test_skip(self):
        assert after_failure({"subtask_result": {"status": "skipped"}}) == "collect"


# ── after_collect ────────────────────────────────────────────────────────

class TestAfterCollect:
    def test_no_plan(self):
        assert after_collect({"task_plan": None}) == "__end__"

    def test_all_done(self):
        state = {
            "task_plan": _make_plan([_make_subtask(1)]),
            "completed_steps": [1],
            "failed_steps": [],
            "circuit_breaker_count": 0,
        }
        assert after_collect(state) == "finalize"

    def test_circuit_breaker_tripped(self):
        state = {
            "task_plan": _make_plan([_make_subtask(1), _make_subtask(2)]),
            "completed_steps": [],
            "failed_steps": [1],
            "circuit_breaker_count": CIRCUIT_BREAKER_THRESHOLD,
        }
        assert after_collect(state) == "finalize"

    def test_failures_trigger_replan(self):
        state = {
            "task_plan": _make_plan([_make_subtask(1), _make_subtask(2)]),
            "completed_steps": [],
            "failed_steps": [1],
            "circuit_breaker_count": 0,
        }
        assert after_collect(state) == "plan"

    def test_more_ready_sends(self):
        plan = _make_plan([_make_subtask(1), _make_subtask(2)])
        state = {
            "task_plan": plan,
            "completed_steps": [],
            "failed_steps": [],
            "circuit_breaker_count": 0,
        }
        result = after_collect(state)
        assert isinstance(result, list)
        assert len(result) == 2  # both ready
