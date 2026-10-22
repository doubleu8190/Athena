from __future__ import annotations

from application.orchestration.root_workflow import render_plan_results


def test_render_plan_results_extracts_text_from_worker_result_envelopes() -> None:
    content = render_plan_results(
        {
            "task-1": {
                "status": "completed",
                "error": None,
                "output": {
                    "output": {"value": "A concise worker answer."},
                    "metrics": {},
                    "evidence": [{"value": "Source-backed evidence."}],
                },
            }
        },
        [{"task_id": "task-1", "title": "Summarize the result"}],
    )

    assert content == "Summarize the result：A concise worker answer."
    assert "metrics" not in content
    assert "evidence" not in content


def test_render_plan_results_keeps_explicit_task_errors_readable() -> None:
    content = render_plan_results(
        {"task-2": {"status": "failed", "output": None, "error": "Needs input."}}
    )

    assert content == "task-2：Needs input."
