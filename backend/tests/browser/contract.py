"""浏览器 E2E harness 与前端共享的类型化 manifest 契约。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _ContractModel(BaseModel):
    """共享严格不可变契约配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AccountManifest(_ContractModel):
    """浏览器真实登录所需的测试账号。"""

    email: str
    password: str = Field(repr=False)
    display_name: str


class SkillManifest(_ContractModel):
    """技能导入和版本更新测试资料。"""

    zip_path: str
    version_two_zip_path: str
    slug: str
    name: str
    guide_marker: str
    script_path: str


class InputManifest(_ContractModel):
    """会话文件上传测试资料。"""

    csv_path: str
    filename: str
    expected_text: str


class ProviderManifest(_ContractModel):
    """本轮 LLM 来源及能力声明。"""

    mode: str
    base_url: str | None
    real_llm_verified: bool
    streaming: bool
    tools: bool
    note: str


class ScenarioManifest(_ContractModel):
    """单个浏览器场景的稳定提示词和预期结果。"""

    name: str
    prompt: str
    approval_decision: str
    expected_terminal_status: str
    expected_artifacts: tuple[str, ...]


class ResourcesManifest(_ContractModel):
    """不含秘密的隔离资源标识。"""

    database_schema: str
    minio_bucket: str
    redis_container: str
    skill_temp_root: str


class BrowserStateManifest(_ContractModel):
    """前后端 browser harness 的唯一状态文件契约。"""

    contract_version: int = 1
    state_file: str
    frontend_base_url: str
    api_base_url: str
    health_url: str
    accounts: dict[str, AccountManifest]
    skill: SkillManifest
    input: InputManifest
    provider: ProviderManifest
    scenarios: dict[str, ScenarioManifest]
    resources: ResourcesManifest
    stop_request_path: str
    ready_at: str


def write_state_manifest(path: Path, manifest: BrowserStateManifest) -> None:
    """原子写入仅当前用户可读的状态文件，避免凭据泄露。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def read_state_manifest(path: Path) -> BrowserStateManifest:
    """严格读取 browser harness 状态文件。"""
    return BrowserStateManifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_json_evidence(path: Path, value: dict[str, object]) -> None:
    """写入脱敏清理证据。"""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


__all__ = [
    "AccountManifest",
    "BrowserStateManifest",
    "InputManifest",
    "ProviderManifest",
    "ResourcesManifest",
    "ScenarioManifest",
    "SkillManifest",
    "read_state_manifest",
    "write_json_evidence",
    "write_state_manifest",
]
