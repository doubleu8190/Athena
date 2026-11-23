"""集中注册所有 SQLAlchemy 模型，供迁移和测试建表使用。"""

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.base import Base
from adapter.file.model import SessionFileModel
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeRequestModel,
    KnowledgeSourceModel,
    KnowledgeUploadReservationModel,
    KnowledgeUploadSlotModel,
    KnowledgeVersionModel,
    ProjectKnowledgeBaseModel,
)
from adapter.llm.model import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from adapter.preference.model import UserMemoryModel, UserWorkProfileModel
from adapter.project.model import ProjectModel
from adapter.run.model import (
    RunEventModel,
    RunInputRequestModel,
    RunModel,
    RunRequestModel,
    RunToolCallModel,
    SessionContextSummaryModel,
)
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel, SkillModel, SkillVersionModel
from adapter.subagent.model import SubagentModel
from adapter.system_prompt.model import (
    SystemPromptSettingModel,
    SystemPromptVersionModel,
)

__all__ = [
    "AuthSessionModel",
    "Base",
    "ConversationMessageModel",
    "DepartmentModel",
    "DeptAdminScopeModel",
    "ExecutionMessageModel",
    "DefaultModelProfileModel",
    "CompressionPolicyVersionModel",
    "CurrentCompressionPolicyModel",
    "ModelProfileVersionModel",
    "McpServerModel",
    "McpToolModel",
    "KnowledgeBaseModel",
    "KnowledgeChunkModel",
    "KnowledgeDocumentModel",
    "KnowledgeJobModel",
    "KnowledgeRequestModel",
    "KnowledgeSourceModel",
    "KnowledgeUploadReservationModel",
    "KnowledgeUploadSlotModel",
    "KnowledgeVersionModel",
    "ProjectKnowledgeBaseModel",
    "ProjectModel",
    "RunEventModel",
    "RunInputRequestModel",
    "RunModel",
    "RunRequestModel",
    "RunToolCallModel",
    "SessionFileModel",
    "SessionContextSummaryModel",
    "SessionModel",
    "SkillExecutionModel",
    "SkillModel",
    "SkillVersionModel",
    "SubagentModel",
    "SystemPromptSettingModel",
    "SystemPromptVersionModel",
    "UserDepartmentModel",
    "UserMemoryModel",
    "UserModel",
    "UserWorkProfileModel",
]
