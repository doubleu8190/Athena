"""The ORM keeps execution authority out of business queue records."""

import pytest

from domain.orchestration.worker_execution_messages import (
    WorkerExecutionEntryType,
    WorkerExecutionMessageAppend,
    WorkerExecutionMessageContent,
    WorkerLLMRole,
)
from tests.fakes.leases import lease_grant
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    ExecutionLeaseModel,
    KnowledgeDocumentJobModel,
    MemoryProcessingJobModel,
    OrchestrationOutboxModel,
    RootResumeJobModel,
    TaskExecutionModel,
    WorkerJobModel,
)


def test_worker_business_models_keep_only_business_state_fields() -> None:
    models = (
        AgentRunModel,
        TaskExecutionModel,
        WorkerJobModel,
        RootResumeJobModel,
        OrchestrationOutboxModel,
        KnowledgeDocumentJobModel,
        MemoryProcessingJobModel,
    )
    removed_lease_fields = {
        "execution_lease_owner",
        "execution_lease_until",
        "lease_owner",
        "lease_until",
        "fencing_token",
    }

    for model in models:
        assert removed_lease_fields.isdisjoint(model.__table__.columns.keys())


def test_execution_leases_use_a_string_resource_key_and_bigint_token() -> None:
    table = ExecutionLeaseModel.__table__

    assert {column.name for column in table.primary_key.columns} == {
        "resource_type",
        "resource_id",
    }
    assert table.c.fencing_token.type.python_type is int
    assert table.c.resource_type.type.python_type is str


def test_worker_message_rejects_another_executions_grant() -> None:
    """执行消息不能绑定其他 Worker 执行的 grant。"""
    with pytest.raises(ValueError, match="worker_message_lease_identity_mismatch"):
        WorkerExecutionMessageAppend(
            message_id="message", session_id="session", run_id="run", plan_id="plan",
            task_id="task", execution_id="execution", execution_generation=1,
            lease=lease_grant("worker_execution", "other"), invocation_id="call",
            entry_type=WorkerExecutionEntryType.LLM_MESSAGE, role=WorkerLLMRole.ASSISTANT,
            content=WorkerExecutionMessageContent(content="hello"),
        )
