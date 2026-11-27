"""Run 执行前知识库范围解析、Skill 上下文重建与输入文件/视觉上下文构建。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.access import AccessPolicyService
from app.file.service import FileInputError, SessionFileService
from app.project.knowledge import KnowledgeNotFoundError as ProjectKnowledgeNotFoundError
from app.project.knowledge import ProjectKnowledgeService
from app.skill.service import SkillService
from domain.access import ResourceAccessTarget, ResourceType, UserAccessSnapshot, VisibilityScope
from domain.file import MAX_VISION_IMAGES_PER_RUN, VISION_IMAGE_MIME_TYPES, VisionImageError
from domain.project import ProjectNotFoundError
from domain.run import Run, RunInputFileContext, RunInputImageAttachment
from domain.skill import RunSkillPromptContext, RunSkillSnapshot

MAX_KNOWLEDGE_SEARCH_BASES = 20

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeScopeResolution:
    """统一知识库范围解析结果：effective_ids 为本次检索实际生效的知识库集合。"""

    effective_ids: tuple[str, ...]
    error_code: str | None = None
    error_message: str | None = None


class RunToolContextBuilder:
    """构建 Run 相关的知识库有效范围、Skill 提示上下文及输入文件/视觉上下文。"""

    def __init__(
        self,
        *,
        project_knowledge: ProjectKnowledgeService | None,
        skills: SkillService | None,
        session_files: SessionFileService | None,
        access_policy: AccessPolicyService,
    ) -> None:
        """保存项目知识库、技能、会话文件与权限策略端口。"""
        self._project_knowledge = project_knowledge
        self._skills = skills
        self._session_files = session_files
        self._access_policy = access_policy

    async def resolve_knowledge_scope(
        self,
        owner_id: str,
        project_id: str | None,
        knowledge_base_ids: tuple[str, ...] | None,
        *,
        strict_project_limit: bool,
        ignore_missing_project: bool = False,
        snapshot: UserAccessSnapshot | None = None,
        snapshot_loaded: bool = False,
    ) -> KnowledgeScopeResolution:
        """统一解析本次对话的有效知识库集合，供快照构建与 knowledge_search 共用。

        模式说明：
        1. 显式模式（knowledge_base_ids is not None）：
           - 空元组表示显式关闭知识库检索；
           - 非空列表经 AccessPolicyService 实时过滤为当前仍未删除且可读的集合（上限 20）。
        2. 默认企业工作台模式（knowledge_base_ids is None）：
           - 计算并集：当前项目关联的可读知识库（若 project_id 非空）
             ∪ 当前用户有权访问的 company 与 department 共享知识库（不自动混入未绑项目的个人库）；
           - 按确定优先级去重排序（项目绑定库 > 部门库 > 公司库），超过 20 个时截取前 20 个。
        """
        if knowledge_base_ids is not None:
            ordered_ids = tuple(dict.fromkeys(knowledge_base_ids))
            if not ordered_ids:
                return KnowledgeScopeResolution(
                    (),
                    "knowledge_unavailable",
                    "当前对话已关闭知识库检索。",
                )
            if len(ordered_ids) > MAX_KNOWLEDGE_SEARCH_BASES:
                return KnowledgeScopeResolution(
                    (),
                    "knowledge_scope_unavailable",
                    "指定的知识库数量超过单次检索上限。",
                )
            if snapshot_loaded and (snapshot is None or not snapshot.is_active):
                readable_ids: set[str] = set()
            else:
                readable_ids = await self._access_policy.filter_readable_ids(
                    owner_id,
                    ResourceType.KNOWLEDGE_BASE,
                    ordered_ids,
                    snapshot=snapshot,
                )
            effective = tuple(kb_id for kb_id in ordered_ids if kb_id in readable_ids)
            if not effective:
                return KnowledgeScopeResolution(
                    (),
                    "knowledge_scope_unavailable",
                    "指定的知识库已不可用或当前用户已失去访问权限。",
                )
            return KnowledgeScopeResolution(effective)

        # 默认企业工作台模式：先收集项目绑定库，再合并企业共享库（部门库 > 公司库）。
        resolved_snapshot = snapshot
        resolved_loaded = snapshot_loaded
        project_ids: list[str] = []
        if project_id is not None and self._project_knowledge is not None:
            if not resolved_loaded and resolved_snapshot is None:
                resolved_snapshot = await self._access_policy.get_user_snapshot(owner_id)
                resolved_loaded = True
            try:
                project_bases = await self._project_knowledge.list(
                    owner_id,
                    project_id,
                    snapshot=resolved_snapshot,
                )
            except (ProjectNotFoundError, ProjectKnowledgeNotFoundError):
                if not ignore_missing_project:
                    raise
                project_bases = []
            if strict_project_limit and len(project_bases) > MAX_KNOWLEDGE_SEARCH_BASES:
                return KnowledgeScopeResolution(
                    (),
                    "knowledge_scope_unavailable",
                    "项目关联的知识库超过单次检索上限。",
                )
            project_ids = [base.id for base in project_bases]

        readable_targets: tuple[ResourceAccessTarget, ...]
        if resolved_loaded and (resolved_snapshot is None or not resolved_snapshot.is_active):
            readable_targets = ()
        else:
            readable_targets = await self._access_policy.filter_readable_targets(
                owner_id,
                ResourceType.KNOWLEDGE_BASE,
                snapshot=resolved_snapshot,
            )
        # 仅自动纳入 company 与 department 共享知识库，未绑定项目的个人库不自动混入。
        dept_ids = sorted(
            target.resource_id
            for target in readable_targets
            if target.visibility_scope is VisibilityScope.DEPARTMENT
        )
        company_ids = sorted(
            target.resource_id
            for target in readable_targets
            if target.visibility_scope is VisibilityScope.COMPANY
        )

        all_ordered = tuple(dict.fromkeys((*project_ids, *dept_ids, *company_ids)))
        if not all_ordered:
            return KnowledgeScopeResolution(
                (),
                "knowledge_unavailable",
                "当前对话没有可查询的知识库。",
            )
        return KnowledgeScopeResolution(all_ordered[:MAX_KNOWLEDGE_SEARCH_BASES])

    async def build_skill_context(
        self,
        run: Run,
        loaded_skill_ids: tuple[str, ...],
    ) -> RunSkillPromptContext:
        """从 Run 固定快照和持久化成功加载证据重建指南，并按实时权限剔除已失权技能。"""
        # 空固定目录表示 Run 未启用 Skill，不要求装配 SkillService。
        if run.skill_snapshot is None or not run.skill_snapshot.skills:
            return RunSkillPromptContext()
        if self._skills is None:
            raise ValueError("skill_runtime_unavailable")
        effective_snapshot = run.skill_snapshot
        readable_ids = await self._access_policy.filter_readable_ids(
            run.owner_id,
            ResourceType.SKILL,
            [item.skill_id for item in effective_snapshot.skills],
        )
        filtered_skills = tuple(
            item for item in effective_snapshot.skills if item.skill_id in readable_ids
        )
        if not filtered_skills:
            return RunSkillPromptContext()
        effective_snapshot = RunSkillSnapshot(
            skills=filtered_skills,
            selected_skill_ids=tuple(
                sid for sid in effective_snapshot.selected_skill_ids if sid in readable_ids
            ),
        )
        selected = set(effective_snapshot.selected_skill_ids)
        loaded = set(loaded_skill_ids)
        active_ids = tuple(
            item.skill_id
            for item in effective_snapshot.skills
            if item.skill_id in selected or item.skill_id in loaded
        )
        return await self._skills.prompt_context(effective_snapshot, active_ids)

    async def build_run_input_file_context(
        self,
        run: Run,
    ) -> tuple[RunInputFileContext, ...]:
        """解析 Run 累计绑定文件；任何失效或跨会话引用都阻止模型执行。"""
        file_ids = run.input_files.file_ids
        if not file_ids:
            return ()
        if self._session_files is None:
            raise FileInputError("session_file_runtime_unavailable")
        files = await self._session_files.get_many(
            run.session_id,
            run.owner_id,
            file_ids,
        )
        return tuple(
            RunInputFileContext(
                file_id=file.id,
                filename=file.filename,
                label=file.label,
                mime_type=file.mime_type,
                size_bytes=file.size_bytes,
                source=file.source.value,
                extraction_status=file.extraction_status.value,
                extracted_text_bytes=file.extracted_text_bytes,
                extraction_error_code=file.extraction_error_code,
            )
            for file in files
        )

    async def build_run_input_vision_context(
        self,
        run: Run,
        input_files: Sequence[RunInputFileContext],
        *,
        supports_vision: bool,
    ) -> tuple[tuple[RunInputFileContext, ...], tuple[RunInputImageAttachment, ...]]:
        """按模型识图能力解析输入图片：非识图温柔降级，识图最多加载 5 张且单图失败不阻断。"""
        if not input_files:
            return (), ()
        image_file_ids = tuple(
            dict.fromkeys(
                item.file_id for item in input_files if item.mime_type in VISION_IMAGE_MIME_TYPES
            )
        )
        if not image_file_ids:
            return tuple(input_files), ()

        # 方案 A2（非识图模型温柔降级）：不读取对象存储图片字节，仅回填安全状态交由上下文引导。
        if not supports_vision:
            updated = tuple(
                replace(item, vision_status="unsupported_by_current_model")
                if item.mime_type in VISION_IMAGE_MIME_TYPES
                else item
                for item in input_files
            )
            return updated, ()

        if self._session_files is None:
            updated = tuple(
                replace(item, vision_status="image_invalid")
                if item.mime_type in VISION_IMAGE_MIME_TYPES
                else item
                for item in input_files
            )
            return updated, ()

        # 批量按当前会话权限复核目标图片元数据，确保只读取当前用户可见的本会话图片。
        session_files = await self._session_files.get_many(
            run.session_id,
            run.owner_id,
            image_file_ids,
        )
        file_by_id = {file.id: file for file in session_files}

        updated_contexts: list[RunInputFileContext] = []
        attachments: list[RunInputImageAttachment] = []
        for item in input_files:
            if item.mime_type not in VISION_IMAGE_MIME_TYPES:
                updated_contexts.append(item)
                continue
            # 单次 Run 最多加载 5 张有效视觉图片；超出上限的后续图片直接跳过并标记 skipped_limit。
            if len(attachments) >= MAX_VISION_IMAGES_PER_RUN:
                updated_contexts.append(replace(item, vision_status="skipped_limit"))
                continue
            session_file = file_by_id.get(item.file_id)
            if session_file is None:
                updated_contexts.append(replace(item, vision_status="image_invalid"))
                continue
            try:
                normalized = await self._session_files.load_vision_image(session_file)
            except asyncio.CancelledError:
                raise
            except VisionImageError as error:
                # 单张损坏、超大或非白名单图片绝不阻断整轮 Run，降级记录安全状态后继续处理后续图片。
                status = (
                    "image_too_large" if error.code == "vision_image_too_large" else "image_invalid"
                )
                updated_contexts.append(replace(item, vision_status=status))
                continue
            except Exception as error:
                logger.warning(
                    "run_vision_image_load_failed run_id=%s file_id=%s exception_type=%s",
                    run.id,
                    item.file_id,
                    type(error).__name__,
                )
                updated_contexts.append(replace(item, vision_status="image_invalid"))
                continue
            attachments.append(
                RunInputImageAttachment(
                    file_id=normalized.file_id,
                    filename=normalized.filename,
                    mime_type=normalized.mime_type,
                    data_base64=normalized.data_base64,
                    width=normalized.width,
                    height=normalized.height,
                )
            )
            updated_contexts.append(replace(item, vision_status="attached"))
        return tuple(updated_contexts), tuple(attachments)
