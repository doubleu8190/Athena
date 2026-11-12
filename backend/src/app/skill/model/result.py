"""Skill 管理查询和资源读取的应用结果。"""

from __future__ import annotations

from dataclasses import dataclass

from domain.skill import Skill, SkillVersion


@dataclass(frozen=True, slots=True)
class SkillAdminDetails:
    """管理员详情所需的稳定身份及一页历史版本。"""

    skill: Skill
    versions: tuple[SkillVersion, ...]


@dataclass(frozen=True, slots=True)
class SkillResourceSlice:
    """技能包文本资源的严格 UTF-8 分页。"""

    skill_id: str
    skill_version_id: str
    path: str
    offset: int
    next_offset: int
    content: str
    eof: bool
    size_bytes: int


__all__ = ["SkillAdminDetails", "SkillResourceSlice"]
