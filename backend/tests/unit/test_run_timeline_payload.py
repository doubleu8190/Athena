from application.runs.query_service import _safe_payload


def test_safe_payload_keeps_complete_worker_result_summary() -> None:
    result = '{"summary":"通过","content":"完整审核意见"}'

    safe = _safe_payload({"result_summary": result, "worker_name": "Worker A"})

    assert safe["result_summary"] == result
    assert safe["worker_name"] == "Worker A"


def test_safe_payload_bounds_large_result_summary() -> None:
    safe = _safe_payload({"result_summary": "x" * 13000})

    assert len(safe["result_summary"]) == 12001
    assert safe["result_summary"].endswith("…")
