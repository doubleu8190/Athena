from athena.runtime.state import (
    derive_execution_state,
    merge_execution_state,
    split_execution_state,
)


def test_execution_state_classifies_checkpoint_and_derived_fields():
    recoverable, derived = split_execution_state(
        {
            "recoverable": {
                "messages": [{"role": "user", "content": "hello"}],
                "turn_count": 2,
                "stream_offset": 12,
            },
            "derived": {"status": "running", "route": "llm_call", "retryable": True},
        }
    )

    assert recoverable == {
        "messages": [{"role": "user", "content": "hello"}],
        "turn_count": 2,
        "stream_offset": 12,
    }
    assert derived == {
        "status": "running",
        "route": "llm_call",
        "retryable": True,
    }


def test_derived_execution_status_prioritizes_interrupt_and_approval():
    assert derive_execution_state({}, {"interrupted": True})["status"] == "interrupted"
    assert derive_execution_state(
        {"approval_batch": {"batch_id": "b1"}, "approval_decisions": {}}, {}
    )["status"] == "waiting_approval"
    assert derive_execution_state({}, {"status": "failed"})["status"] == "failed"


def test_execution_reducer_merges_recoverable_and_derived_containers_independently():
    merged = merge_execution_state(
        {
            "recoverable": {"messages": [], "tool_result_deltas": [{"id": "a"}]},
            "derived": {"status": "running"},
        },
        {
            "recoverable": {"tool_result_deltas": [{"id": "b"}]},
            "derived": {"route": "finish_execution"},
        },
    )

    assert merged == {
        "recoverable": {
            "messages": [],
            "tool_result_deltas": [{"id": "a"}, {"id": "b"}],
        },
        "derived": {"status": "running", "route": "finish_execution"},
    }
