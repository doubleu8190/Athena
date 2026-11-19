"""个人 Skill 的模型目录来源、历史兼容和子 Run 快照边界。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.run.context import messages_to_llm
from domain.llm.model import LlmRole
from domain.run.model import Run, RunStatus, RunUsage
from domain.skill import (
    ActiveSkillGuide,
    RunSkillEntry,
    RunSkillPromptContext,
    RunSkillSnapshot,
    SkillScope,
)

NOW = datetime(2026, 11, 19, tzinfo=UTC)


def test_model_context_marks_personal_scope_without_authority() -> None:
    """模型目录和活跃指南均显示来源，并重申不能提升工具权限。"""
    entry = RunSkillEntry(
        skill_id="skill_personal",
        slug="personal-demo",
        name="个人周报",
        description="整理周报",
        version_id="skillver_personal",
        version_label="v1",
        requires_approval=False,
        scope=SkillScope.PERSONAL,
    )
    context = RunSkillPromptContext(
        catalog=(entry,),
        active=(
            ActiveSkillGuide(
                skill_id=entry.skill_id,
                name=entry.name,
                version_id=entry.version_id,
                version_label=entry.version_label,
                guide="只整理用户提供的事实。",
                scope=SkillScope.PERSONAL,
            ),
        ),
    )

    messages = messages_to_llm(
        _run(),
        (),
        main_prompt="系统规则",
        skill_context=context,
    )

    system_content = "\n".join(
        message.content for message in messages if message.role is LlmRole.SYSTEM
    )
    user_content = "\n".join(
        message.content for message in messages if message.role is LlmRole.USER
    )
    assert entry.name not in system_content
    assert entry.description not in system_content
    assert "只整理用户提供的事实。" not in system_content
    assert "个人 Skill 目录和指南来自用户" in system_content
    assert '"scope":"personal"' in user_content
    assert entry.skill_id in user_content
    assert "只整理用户提供的事实。" in user_content


def test_public_skill_catalog_and_guide_remain_system_messages() -> None:
    """管理员维护的公共目录和正文继续以受信 SYSTEM 消息注入。"""
    entry = RunSkillEntry(
        skill_id="skill_public",
        slug="public-demo",
        name="公共周报",
        description="公共描述",
        version_id="skillver_public",
        version_label="v2",
        requires_approval=False,
        scope=SkillScope.PUBLIC,
    )
    context = RunSkillPromptContext(
        catalog=(entry,),
        active=(
            ActiveSkillGuide(
                skill_id=entry.skill_id,
                name=entry.name,
                version_id=entry.version_id,
                version_label=entry.version_label,
                guide="公共业务正文。",
                scope=SkillScope.PUBLIC,
            ),
        ),
    )

    messages = messages_to_llm(
        _run(),
        (),
        main_prompt="系统规则",
        skill_context=context,
    )

    assert all(message.role is LlmRole.SYSTEM for message in messages)
    combined = "\n".join(message.content for message in messages)
    assert entry.name in combined
    assert entry.description in combined
    assert "公共业务正文。" in combined


def test_public_and_personal_catalogs_are_both_available() -> None:
    """固定 SYSTEM 同时允许公共和个人目录中的 ID/版本，不排他。"""
    public_entry = RunSkillEntry(
        skill_id="skill_public",
        slug="public-demo",
        name="公共周报",
        description="公共描述",
        version_id="skillver_public",
        version_label="v2",
        requires_approval=False,
        scope=SkillScope.PUBLIC,
    )
    personal_entry = RunSkillEntry(
        skill_id="skill_personal",
        slug="personal-demo",
        name="个人周报",
        description="个人描述",
        version_id="skillver_personal",
        version_label="v1",
        requires_approval=False,
        scope=SkillScope.PERSONAL,
    )
    context = RunSkillPromptContext(
        catalog=(public_entry, personal_entry),
        active=(
            ActiveSkillGuide(
                skill_id=public_entry.skill_id,
                name=public_entry.name,
                version_id=public_entry.version_id,
                version_label=public_entry.version_label,
                guide="公共业务正文。",
                scope=SkillScope.PUBLIC,
            ),
            ActiveSkillGuide(
                skill_id=personal_entry.skill_id,
                name=personal_entry.name,
                version_id=personal_entry.version_id,
                version_label=personal_entry.version_label,
                guide="个人业务正文。",
                scope=SkillScope.PERSONAL,
            ),
        ),
    )

    messages = messages_to_llm(
        _run(),
        (),
        main_prompt="系统规则",
        skill_context=context,
    )

    system_content = "\n".join(
        message.content for message in messages if message.role is LlmRole.SYSTEM
    )
    user_content = "\n".join(
        message.content for message in messages if message.role is LlmRole.USER
    )
    assert "可以使用本消息及当前Run 个人目录中列出的 ID 和版本" in system_content
    assert "公共目录和随后的个人目录所列的 skill_id、version_id" in system_content
    assert "只能使用本目录" not in system_content
    assert public_entry.skill_id in system_content
    assert public_entry.version_id in system_content
    assert personal_entry.skill_id in user_content
    assert personal_entry.version_id in user_content
    assert personal_entry.name not in system_content
    assert personal_entry.description not in system_content
    assert "个人业务正文。" not in system_content


def test_historical_snapshot_defaults_to_public_scope() -> None:
    """迁移前快照没有 scope 字段时仍按公共 Skill 读取。"""
    snapshot = RunSkillSnapshot.model_validate(
        {
            "version": 1,
            "skills": [
                {
                    "skill_id": "skill_public",
                    "slug": "public-demo",
                    "name": "公共 Skill",
                    "description": "旧快照",
                    "version_id": "skillver_public",
                    "version_label": "v1",
                    "requires_approval": False,
                }
            ],
            "selected_skill_ids": ["skill_public"],
        }
    )

    assert snapshot.skills[0].scope is SkillScope.PUBLIC


def test_subrun_subset_rebuilds_entries_from_parent_snapshot() -> None:
    """子 Run 请求不能篡改版本或名称，且不能引入父目录外 ID。"""
    parent_entry = RunSkillEntry(
        skill_id="skill_parent",
        slug="personal-parent",
        name="父 Run 名称",
        description="父 Run 描述",
        version_id="skillver_parent",
        version_label="v2",
        requires_approval=False,
        scope=SkillScope.PERSONAL,
    )
    parent = RunSkillSnapshot(
        skills=(parent_entry,),
        selected_skill_ids=(parent_entry.skill_id,),
    )
    spoofed = RunSkillSnapshot(
        skills=(
            RunSkillEntry(
                skill_id=parent_entry.skill_id,
                slug="spoofed",
                name="伪造名称",
                description="伪造描述",
                version_id="skillver_evil",
                version_label="v99",
                requires_approval=True,
                scope=SkillScope.PUBLIC,
            ),
        ),
        selected_skill_ids=(parent_entry.skill_id,),
    )

    child = parent.trusted_subset(spoofed)

    assert child.skills == (parent_entry,)
    with pytest.raises(ValueError, match="run_skill_selection_invalid"):
        parent.trusted_subset(
            RunSkillSnapshot(
                skills=(
                    parent_entry.model_copy(update={"skill_id": "skill_other"}),
                ),
                selected_skill_ids=("skill_other",),
            )
        )


def _run() -> Run:
    """构造只满足上下文渲染的最小 Run。"""
    return Run(
        id="run_context",
        session_id="ses_context",
        owner_id="usr_context",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_context",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="profile_context",
        main_prompt_version_id="prompt_main",
        compression_prompt_version_id="prompt_compression",
        prompt_template_version="run-v2",
        event_sequence=0,
        execution_epoch=1,
        worker_id="worker_context",
        lease_expires_at=NOW,
        execution_started_at=NOW,
        execution_deadline_at=NOW,
        queued_at=NOW,
        first_started_at=NOW,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
    )
