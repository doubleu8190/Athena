"""Tests for ExecutionState schema and reducers."""

from athena.core.graph.state import ExecutionState, _merge_dicts


class TestMergeDictsReducer:
    """Tests for the custom _merge_dicts reducer used by step_outputs."""

    def test_merge_empty_left(self):
        result = _merge_dicts({}, {"step1": "data"})
        assert result == {"step1": "data"}

    def test_merge_empty_right(self):
        result = _merge_dicts({"step1": "data"}, {})
        assert result == {"step1": "data"}

    def test_merge_adds_new_key(self):
        result = _merge_dicts({"step1": "a"}, {"step2": "b"})
        assert result == {"step1": "a", "step2": "b"}

    def test_merge_overwrites_existing_key(self):
        result = _merge_dicts({"step1": "old"}, {"step1": "new"})
        assert result == {"step1": "new"}

    def test_merge_preserves_other_keys(self):
        result = _merge_dicts({"step1": "a", "step2": "b"}, {"step1": "new"})
        assert result == {"step1": "new", "step2": "b"}

    def test_merge_multiple_updates(self):
        base = {"step1": "a"}
        r1 = _merge_dicts(base, {"step2": "b"})
        r2 = _merge_dicts(r1, {"step3": "c"})
        assert r2 == {"step1": "a", "step2": "b", "step3": "c"}

    def test_merge_nested_values(self):
        """Reducer does shallow merge — nested values replace wholesale."""
        result = _merge_dicts(
            {"step1": {"deep": "old", "keep": True}},
            {"step1": {"deep": "new"}},
        )
        assert result == {"step1": {"deep": "new"}}


class TestExecutionStateTypedDict:
    """Verify that the ExecutionState TypedDict is well-formed."""

    def test_minimal_state(self):
        """A minimal state dict should have all required fields resolvable."""
        from langchain_core.messages import HumanMessage

        state: ExecutionState = {
            "messages": [HumanMessage(content="hello")],
            "system_context": None,
            "task_plan": None,
            "plan_error": None,
            "step_outputs": {},
            "completed_steps": [],
            "failed_steps": [],
            "subtask_result": None,
            "circuit_breaker_count": 0,
            "retry_counts": {},
            "status": "planning",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }
        # Just verify keys exist
        assert state["status"] == "planning"
        assert state["circuit_breaker_count"] == 0
        assert state["task_plan"] is None
