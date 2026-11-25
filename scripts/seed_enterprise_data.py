#!/usr/bin/env python3
"""Athena v2 企业场景测试数据体系（V1 + V2 + V3）一键幂等导入、状态复位与在线量化验收 CLI。

核心原则：
1. 严格幂等：重复执行不重复创建知识库（6 库 36 档）、文档、显式记忆、MCP Server（3 套 16 工具）、
   Skill（9 个）、Subagent（3 个）或专项 Project（2 个），默认保留 ``chat_explicit`` 记忆。
2. 同源安全：所有写请求均携带与 ``--base-url`` 同源的 ``Origin`` 头，
   并为 6 个 Mock 角色维护彼此隔离的 Cookie Jar。
3. 量化验收：``--verify`` 执行多角色 RBAC/资产验收与 110 题检索量化评测（Hit@1/5/8、MRR、零泄漏），
   支持通过 ``--eval-report <path>`` 写出报告及 ``--verify-org-lifecycle`` 演练停用吊销与恢复。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from datasets.enterprise_hroa.builders.build_assets import (  # noqa: E402
    DATASET_ROOT,
    GENERATED_DIR,
    build_all_assets,
)
from mcp_servers.hroa.state import HroaStateStore  # noqa: E402

MANIFEST_PATH = DATASET_ROOT / "manifest.yaml"
EVAL_FIXTURE_PATH = (
    REPO_ROOT / "backend" / "tests" / "fixtures" / "enterprise_knowledge_retrieval_eval.json"
)
ALL_MODULES = ("knowledge", "memory", "mcp", "skills", "subagents", "projects")

MIME_OVERRIDES: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".zip": "application/zip",
}


def _sha256_bytes(data: bytes) -> str:
    """计算字节流 SHA-256 十六进制摘要，用于内容比对与幂等键派生。"""
    return hashlib.sha256(data).hexdigest()


def _canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    """对规范化 JSON 请求体计算确定性 SHA-256，供创建与发布幂等键完整覆盖全部业务字段。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _make_idempotency_key(*parts: str) -> str:
    """构造严格符合 Athena 契约（仅可见 ASCII 且长度 <= 128）的稳定 Idempotency-Key。"""
    sanitized = [
        "".join(ch if 0x21 <= ord(ch) <= 0x7E else "-" for ch in part.strip())
        for part in parts
        if part.strip()
    ]
    key = "-".join(sanitized)
    if len(key) <= 128:
        return key
    digest = _sha256_bytes(key.encode("utf-8"))[:24]
    return f"{key[:103]}-{digest}"


def reset_hroa_state_safely(
    *,
    mcp_runtime: str,
    state_file: str | None,
    docker_container: str | None,
    dry_run: bool,
    in_container: bool | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, object]:
    """按运行环境安全复位 HR/OA 共享状态文件，严禁在宿主机误写 Docker 容器路径 /app/...。"""
    running_in_container = Path("/.dockerenv").exists() if in_container is None else in_container
    default_state = (
        "/app/var/hroa-test/state.json"
        if mcp_runtime == "docker"
        else str(REPO_ROOT / "var" / "hroa-test" / "state.json")
    )
    target_state = (state_file or default_state).strip()
    is_container_path = target_state == "/app" or target_state.startswith("/app/")

    if mcp_runtime == "docker" and not running_in_container:
        # 显式传入 --docker-container 时，无论路径是否以 /app/ 开头均经 docker exec 复位。
        if docker_container and docker_container.strip():
            container = docker_container.strip()
            cmd = [
                "docker",
                "exec",
                container,
                "/usr/local/bin/python",
                "/app/mcp_servers/hroa/mcp_server.py",
                "--state-file",
                target_state,
                "--reset-state",
            ]
            if dry_run:
                print(f"[dry-run] reset HROA state via container: {' '.join(cmd)}")
                return {"status": "dry_run", "mode": "docker_exec", "command": cmd}
            runner(cmd, check=True)
            return {"status": "reset", "mode": "docker_exec", "container": container}

        if is_container_path:
            suggested_cmd = (
                "docker exec athena-backend-api /usr/local/bin/python "
                f"/app/mcp_servers/hroa/mcp_server.py --state-file {target_state} --reset-state"
            )
            raise RuntimeError(
                "hroa_reset_requires_docker_container: 当前在宿主机上操作 Docker 容器路径 "
                f"({target_state})，已拒绝在宿主机本地写盘。"
                f"请追加 --docker-container athena-backend-api，或在终端执行：\n  {suggested_cmd}"
            )

    if dry_run:
        print(f"[dry-run] reset HROA state file: {target_state}")
        return {"status": "dry_run", "mode": "local", "state_file": target_state}

    return HroaStateStore(Path(target_state)).reset_state()


def _normalize_text(value: str) -> str:
    """规范化空白以便幂等比对显式记忆或文本内容。"""
    return " ".join(value.strip().split())


def _normalize_grants(
    grants: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[str, str, bool]]:
    """将可见性授权列表规范化为可排序比较的三元组列表。"""
    if not grants:
        return []
    return sorted(
        (
            str(item["target_type"]),
            str(item["target_id"]),
            bool(item.get("include_subdepartments", True)),
        )
        for item in grants
    )


def _origin_from_base_url(base_url: str) -> str:
    """从目标 API 基址提取同源 Origin 头，满足 Athena CSRF 同源保护。"""
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"base_url_invalid:{base_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _mime_for_path(path: Path) -> str:
    """根据扩展名解析稳定 MIME 类型。"""
    suffix = path.suffix.lower()
    if suffix in MIME_OVERRIDES:
        return MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


@dataclass(slots=True)
class SeedSummary:
    """记录一次导入或验收执行的关键统计与告警。"""

    created: dict[str, int] = field(
        default_factory=lambda: {
            "knowledge_bases": 0,
            "documents": 0,
            "document_versions": 0,
            "memories": 0,
            "mcp_servers": 0,
            "skills": 0,
            "subagents": 0,
            "projects": 0,
            "project_kb_bindings": 0,
        }
    )
    updated: dict[str, int] = field(
        default_factory=lambda: {
            "knowledge_bases": 0,
            "preferences": 0,
            "mcp_servers": 0,
            "mcp_tools": 0,
            "skills": 0,
            "subagents": 0,
            "projects": 0,
        }
    )
    skipped: dict[str, int] = field(
        default_factory=lambda: {
            "knowledge_bases": 0,
            "documents": 0,
            "preferences": 0,
            "memories": 0,
            "mcp_servers": 0,
            "mcp_tools": 0,
            "skills": 0,
            "subagents": 0,
            "projects": 0,
            "project_kb_bindings": 0,
        }
    )
    warnings: list[str] = field(default_factory=list)
    verified_checks: list[str] = field(default_factory=list)
    retrieval_eval_report: dict[str, Any] | None = None
    kb_ids_by_key: dict[str, str] = field(default_factory=dict)
    doc_ids_by_code: dict[str, str] = field(default_factory=dict)
    subagent_ids_by_slug: dict[str, str] = field(default_factory=dict)
    project_ids_by_code: dict[str, str] = field(default_factory=dict)


class EnterpriseSeeder:
    """编排多角色登录、六类测试资产幂等导入及在线 RBAC / 110 题检索量化验收。"""

    def __init__(
        self,
        *,
        base_url: str,
        mcp_runtime: str = "docker",
        hroa_state_file: str | None = None,
        skip_kb_ocr: bool = False,
        reset_memories: bool = False,
        dry_run: bool = False,
        wait_timeout: float = 120.0,
        poll_interval: float = 1.0,
        eval_report_path: Path | None = None,
        verify_org_lifecycle: bool = False,
        manifest_path: Path = MANIFEST_PATH,
        eval_fixture_path: Path = EVAL_FIXTURE_PATH,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    ) -> None:
        """初始化导入器配置并加载声明式 manifest.yaml。"""
        self._base_url = base_url.rstrip("/")
        self._origin = _origin_from_base_url(self._base_url)
        self._mcp_runtime = mcp_runtime
        self._hroa_state_file = hroa_state_file
        self._skip_kb_ocr = skip_kb_ocr
        self._reset_memories = reset_memories
        self._dry_run = dry_run
        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval
        self._eval_report_path = eval_report_path
        self._verify_org_lifecycle = verify_org_lifecycle
        self._manifest_path = manifest_path
        self._eval_fixture_path = eval_fixture_path
        self._transport_factory = transport_factory
        self._manifest: dict[str, Any] = cast(
            dict[str, Any],
            yaml.safe_load(manifest_path.read_text(encoding="utf-8")),
        )
        self._clients: dict[str, httpx.Client] = {}
        self.summary = SeedSummary()

    def close(self) -> None:
        """关闭所有角色独立的 HTTP 客户端会话。"""
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def _client_for(self, user_id: str) -> httpx.Client:
        """按 Mock 角色懒登录并缓存独立 Cookie Jar 客户端。"""
        existing = self._clients.get(user_id)
        if existing is not None:
            return existing
        kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": {"Origin": self._origin},
            "timeout": 30.0,
        }
        if self._transport_factory is not None:
            kwargs["transport"] = self._transport_factory()
        client = httpx.Client(**kwargs)
        resp = client.post("/api/v1/auth/mock/login", json={"user_id": user_id})
        if resp.status_code != 200:
            client.close()
            raise RuntimeError(
                f"mock_login_failed user_id={user_id} status={resp.status_code} body={resp.text}"
            )
        self._clients[user_id] = client
        return client

    def _require_status(
        self,
        response: httpx.Response,
        expected: int | tuple[int, ...],
        context: str,
    ) -> httpx.Response:
        """统一断言 HTTP 状态码并在失败时抛出清晰诊断信息。"""
        allowed = (expected,) if isinstance(expected, int) else expected
        if response.status_code not in allowed:
            raise RuntimeError(
                f"api_call_failed context={context} status={response.status_code} "
                f"expected={allowed} body={response.text[:400]}"
            )
        return response

    def _resolve_mcp_stdio_config(self, profile: str) -> dict[str, Any]:
        """根据 docker/host 运行时模式拼装绝对路径 stdio MCP 启动参数。"""
        if self._mcp_runtime == "docker":
            state_path = self._hroa_state_file or "/app/var/hroa-test/state.json"
            return {
                "command": "/usr/local/bin/python",
                "args": [
                    "/app/mcp_servers/hroa/mcp_server.py",
                    "--profile",
                    profile,
                    "--state-file",
                    state_path,
                ],
                "cwd": "/app/mcp_servers/hroa",
            }
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        python_bin = str(venv_python) if venv_python.is_file() else sys.executable
        state_path = self._hroa_state_file or str(REPO_ROOT / "var" / "hroa-test" / "state.json")
        return {
            "command": python_bin,
            "args": [
                str(REPO_ROOT / "mcp_servers" / "hroa" / "mcp_server.py"),
                "--profile",
                profile,
                "--state-file",
                state_path,
            ],
            "cwd": str(REPO_ROOT / "mcp_servers" / "hroa"),
        }

    # ------------------------------------------------------------------
    # 1. 知识库与多格式文档幂等导入
    # ------------------------------------------------------------------

    def _list_all_bases(self, client: httpx.Client) -> list[dict[str, Any]]:
        """分页拉取当前角色可见的全部知识库。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._require_status(
                client.get("/api/v1/knowledge-bases", params={"page": page, "page_size": 100}),
                200,
                "list_knowledge_bases",
            )
            batch = cast(list[dict[str, Any]], resp.json().get("items", []))
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _list_all_documents(self, client: httpx.Client, base_id: str) -> list[dict[str, Any]]:
        """分页拉取指定知识库下的全部文档摘要。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._require_status(
                client.get(
                    f"/api/v1/knowledge-bases/{base_id}/documents",
                    params={"page": page, "page_size": 100},
                ),
                200,
                f"list_documents:{base_id}",
            )
            batch = cast(list[dict[str, Any]], resp.json().get("items", []))
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _poll_document_ready(
        self,
        client: httpx.Client,
        base_id: str,
        doc_id: str,
        *,
        doc_code: str,
        requires_ocr: bool,
    ) -> str:
        """轮询文档最新版本状态直至 ready 或 failed；OCR 样本失败仅记录告警不阻断全局。"""
        deadline = time.monotonic() + self._wait_timeout
        last_state = "unknown"
        while time.monotonic() <= deadline:
            resp = self._require_status(
                client.get(f"/api/v1/knowledge-bases/{base_id}/documents/{doc_id}"),
                200,
                f"poll_document:{doc_code}",
            )
            payload = cast(dict[str, Any], resp.json())
            latest = cast(dict[str, Any] | None, payload.get("latest_version"))
            if latest is None:
                return "ready"
            last_state = str(latest.get("state", "unknown"))
            if last_state == "ready":
                return "ready"
            if last_state == "failed":
                err_code = latest.get("error_code")
                if requires_ocr:
                    self.summary.warnings.append(
                        f"kb_ocr_doc_failed doc={doc_code} error_code={err_code} (不阻断其余资产)"
                    )
                    return "failed_ocr_ignored"
                raise RuntimeError(
                    f"kb_document_failed doc={doc_code} id={doc_id} error_code={err_code}"
                )
            time.sleep(self._poll_interval)
        if requires_ocr:
            self.summary.warnings.append(
                f"kb_ocr_doc_timeout doc={doc_code} last_state={last_state} (不阻断其余资产)"
            )
            return "timeout_ocr_ignored"
        raise RuntimeError(f"kb_document_timeout doc={doc_code} last_state={last_state}")

    def seed_knowledge(self) -> None:
        """幂等创建/对齐 6 个知识库并上传 36 份多格式文档（内容未变则跳过）。"""
        for kb_spec in cast(list[dict[str, Any]], self._manifest["knowledge_bases"]):
            kb_key = str(kb_spec["key"])
            owner_actor = str(kb_spec["owner_actor"])
            name = str(kb_spec["name"])
            description = str(kb_spec["description"])
            scope = str(kb_spec["visibility_scope"])
            owner_dept = kb_spec.get("owner_department_id")
            target_grants = cast(list[dict[str, Any]], kb_spec.get("visibility_grants", []))

            if self._dry_run:
                print(
                    f"[dry-run][knowledge] ensure KB {kb_spec['code']} ({name}) actor={owner_actor}"
                )
                for doc_spec in cast(list[dict[str, Any]], kb_spec["documents"]):
                    if self._skip_kb_ocr and bool(doc_spec.get("requires_ocr")):
                        print(f"[dry-run][knowledge] skip OCR doc {doc_spec['doc_id']}")
                        continue
                    print(
                        f"[dry-run][knowledge] ensure doc {doc_spec['doc_id']} {doc_spec['title']}"
                    )
                continue

            client = self._client_for(owner_actor)
            bases = self._list_all_bases(client)
            matched_base = next(
                (
                    item
                    for item in bases
                    if item.get("name") == name
                    and item.get("visibility_scope") == scope
                    and item.get("owner_department_id") == owner_dept
                ),
                None,
            )

            if matched_base is None:
                body: dict[str, Any] = {
                    "name": name,
                    "description": description,
                    "visibility_scope": scope,
                }
                if scope == "department":
                    body["owner_department_id"] = owner_dept
                    body["visibility_grants"] = target_grants
                resp = self._require_status(
                    client.post("/api/v1/knowledge-bases", json=body),
                    201,
                    f"create_kb:{kb_key}",
                )
                matched_base = cast(dict[str, Any], resp.json())
                self.summary.created["knowledge_bases"] += 1
            else:
                # 仅在描述或授权清单漂移时发送 PATCH，保证幂等重跑零多余写请求。
                existing_grants = _normalize_grants(matched_base.get("visibility_grants"))
                desired_grants = _normalize_grants(target_grants if scope == "department" else [])
                needs_patch = matched_base.get("description") != description or (
                    scope == "department" and existing_grants != desired_grants
                )
                if needs_patch:
                    patch_body: dict[str, Any] = {"name": name, "description": description}
                    if scope == "department":
                        patch_body["visibility_grants"] = target_grants
                    resp = self._require_status(
                        client.patch(
                            f"/api/v1/knowledge-bases/{matched_base['id']}",
                            json=patch_body,
                        ),
                        200,
                        f"patch_kb:{kb_key}",
                    )
                    matched_base = cast(dict[str, Any], resp.json())
                    self.summary.updated["knowledge_bases"] += 1
                else:
                    self.summary.skipped["knowledge_bases"] += 1

            base_id = str(matched_base["id"])
            self.summary.kb_ids_by_key[kb_key] = base_id

            existing_docs = self._list_all_documents(client, base_id)
            docs_by_title = {str(doc["title"]): doc for doc in existing_docs}

            for doc_spec in cast(list[dict[str, Any]], kb_spec["documents"]):
                doc_code = str(doc_spec["doc_id"])
                title = str(doc_spec["title"])
                requires_ocr = bool(doc_spec.get("requires_ocr", False))
                if requires_ocr and self._skip_kb_ocr:
                    self.summary.warnings.append(f"skip_kb_ocr doc={doc_code}")
                    self.summary.skipped["documents"] += 1
                    continue

                file_path = GENERATED_DIR / str(doc_spec["file"])
                if not file_path.is_file():
                    raise FileNotFoundError(
                        f"generated_asset_missing:{file_path} "
                        "(请先运行 build_assets.py 或加 --rebuild-assets)"
                    )
                content = file_path.read_bytes()
                local_sha = _sha256_bytes(content)
                mime_type = _mime_for_path(file_path)

                existing_doc = docs_by_title.get(title)
                if existing_doc is None:
                    idem_key = _make_idempotency_key("seed-kb", kb_key, doc_code, local_sha[:16])
                    resp = self._require_status(
                        client.post(
                            f"/api/v1/knowledge-bases/{base_id}/documents/upload",
                            headers={"Idempotency-Key": idem_key},
                            data={"title": title},
                            files={"file": (file_path.name, content, mime_type)},
                        ),
                        202,
                        f"upload_doc:{doc_code}",
                    )
                    created_doc_id = str(resp.json()["document_id"])
                    self.summary.created["documents"] += 1
                    self.summary.doc_ids_by_code[doc_code] = created_doc_id
                    self._poll_document_ready(
                        client,
                        base_id,
                        created_doc_id,
                        doc_code=doc_code,
                        requires_ocr=requires_ocr,
                    )
                    continue

                existing_doc_id = str(existing_doc["id"])
                self.summary.doc_ids_by_code[doc_code] = existing_doc_id
                latest_version = cast(dict[str, Any] | None, existing_doc.get("latest_version"))
                active_version_id = existing_doc.get("active_version_id")
                latest_state = str(latest_version.get("state")) if latest_version else None

                if latest_state in {"queued", "processing"}:
                    self._poll_document_ready(
                        client,
                        base_id,
                        existing_doc_id,
                        doc_code=doc_code,
                        requires_ocr=requires_ocr,
                    )
                    self.summary.skipped["documents"] += 1
                    continue

                # 比对当前活动版本原件 SHA-256；内容完全一致则严格跳过不发新版。
                same_content = False
                if isinstance(active_version_id, str) and active_version_id:
                    src_resp = client.get(
                        f"/api/v1/knowledge-bases/{base_id}/documents/{existing_doc_id}"
                        f"/versions/{active_version_id}/source"
                    )
                    if src_resp.status_code == 200 and _sha256_bytes(src_resp.content) == local_sha:
                        same_content = True

                if same_content:
                    self.summary.skipped["documents"] += 1
                    continue

                if latest_state == "failed" and requires_ocr:
                    self.summary.warnings.append(
                        f"kb_ocr_doc_already_failed doc={doc_code} (保留既有状态，不阻断)"
                    )
                    self.summary.skipped["documents"] += 1
                    continue

                # 将当前版本标识编入 replace 幂等键，确保 V1->V2->V1->V2->V1 往返均发布新版。
                current_ver_id = (
                    active_version_id
                    if isinstance(active_version_id, str) and active_version_id
                    else (
                        str(latest_version.get("id"))
                        if latest_version and latest_version.get("id")
                        else "none"
                    )
                )
                ver_tag = _sha256_bytes(current_ver_id.encode("utf-8"))[:12]
                idem_key = _make_idempotency_key("seed-kb-ver", doc_code, ver_tag, local_sha[:16])
                self._require_status(
                    client.post(
                        f"/api/v1/knowledge-bases/{base_id}/documents/{existing_doc_id}/versions/upload",
                        headers={"Idempotency-Key": idem_key},
                        files={"file": (file_path.name, content, mime_type)},
                    ),
                    202,
                    f"replace_doc:{doc_code}",
                )
                self.summary.created["document_versions"] += 1
                self._poll_document_ready(
                    client,
                    base_id,
                    existing_doc_id,
                    doc_code=doc_code,
                    requires_ocr=requires_ocr,
                )

    # ------------------------------------------------------------------
    # 2. 6 个 Mock 角色偏好与显式记忆幂等导入
    # ------------------------------------------------------------------

    def seed_memories(self) -> None:
        """幂等对齐 6 角色工作偏好并按差集补齐 manual 记忆（不删 chat_explicit）。"""
        for role_spec in cast(list[dict[str, Any]], self._manifest["preferences_and_memories"]):
            user_id = str(role_spec["user_id"])
            target_instructions = str(role_spec["custom_instructions"]).strip()
            target_enabled = bool(role_spec["memory_enabled"])
            target_memories = [
                str(item).strip() for item in cast(list[Any], role_spec.get("manual_memories", []))
            ]

            if self._dry_run:
                print(
                    f"[dry-run][memory] ensure profile user={user_id} "
                    f"memory_enabled={target_enabled} manual_count={len(target_memories)}"
                )
                continue

            client = self._client_for(user_id)
            prof_resp = self._require_status(
                client.get("/api/v1/me/preferences"),
                200,
                f"get_preferences:{user_id}",
            )
            profile = cast(dict[str, Any], prof_resp.json())
            if (
                str(profile.get("custom_instructions", "")).strip() != target_instructions
                or bool(profile.get("memory_enabled", True)) != target_enabled
            ):
                self._require_status(
                    client.put(
                        "/api/v1/me/preferences",
                        json={
                            "custom_instructions": target_instructions,
                            "memory_enabled": target_enabled,
                        },
                    ),
                    200,
                    f"put_preferences:{user_id}",
                )
                self.summary.updated["preferences"] += 1
            else:
                self.summary.skipped["preferences"] += 1

            mem_resp = self._require_status(
                client.get("/api/v1/me/memories"),
                200,
                f"list_memories:{user_id}",
            )
            existing_items = cast(list[dict[str, Any]], mem_resp.json().get("items", []))
            if self._reset_memories:
                # 仅清理 manual 来源记忆，绝不删除用户对话中保存的 chat_explicit 记忆。
                for item in existing_items:
                    if item.get("source") == "manual":
                        self._require_status(
                            client.delete(f"/api/v1/me/memories/{item['id']}"),
                            204,
                            f"delete_manual_memory:{user_id}",
                        )
                existing_items = [item for item in existing_items if item.get("source") != "manual"]

            existing_normalized = {
                _normalize_text(str(item.get("content", ""))) for item in existing_items
            }
            for text in target_memories:
                if _normalize_text(text) in existing_normalized:
                    self.summary.skipped["memories"] += 1
                    continue
                self._require_status(
                    client.post("/api/v1/me/memories", json={"content": text}),
                    201,
                    f"create_memory:{user_id}",
                )
                self.summary.created["memories"] += 1
                existing_normalized.add(_normalize_text(text))

    # ------------------------------------------------------------------
    # 3. 三 Profile HR/OA MCP Server（16 工具）及 9 写入工具审批元信息幂等导入
    # ------------------------------------------------------------------

    def _list_all_mcp_servers(self, client: httpx.Client) -> list[dict[str, Any]]:
        """分页读取管理员可见的全部 MCP Server。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._require_status(
                client.get("/api/v1/admin/mcp/servers", params={"page": page, "page_size": 100}),
                200,
                "list_mcp_servers",
            )
            batch = cast(list[dict[str, Any]], resp.json().get("items", []))
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _list_all_public_tools(self, client: httpx.Client) -> list[dict[str, Any]]:
        """分页读取当前登录用户可见的全部公开工具目录，避免超过 100 项时截断漏判。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._require_status(
                client.get("/api/v1/tools", params={"page": page, "page_size": 100}),
                200,
                "list_public_tools",
            )
            batch = cast(list[dict[str, Any]], resp.json().get("items", []))
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _list_server_tools(self, client: httpx.Client, server_id: str) -> list[dict[str, Any]]:
        """分页读取指定 MCP Server 下已发现的全部工具。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._require_status(
                client.get(
                    f"/api/v1/admin/mcp/servers/{server_id}/tools",
                    params={"page": page, "page_size": 100},
                ),
                200,
                f"list_server_tools:{server_id}",
            )
            batch = cast(list[dict[str, Any]], resp.json().get("items", []))
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def seed_mcp(self) -> None:
        """幂等注册或更新 3 个 HR/OA MCP Server（16 工具）并配置 9 个写入工具中文审批模板。"""
        for server_spec in cast(list[dict[str, Any]], self._manifest["mcp_servers"]):
            owner_actor = str(server_spec["owner_actor"])
            name = str(server_spec["name"])
            profile = str(server_spec["profile"])
            scope = str(server_spec["visibility_scope"])
            owner_dept = server_spec.get("owner_department_id")
            target_grants = cast(list[dict[str, Any]], server_spec.get("visibility_grants", []))
            stdio_cfg = self._resolve_mcp_stdio_config(profile)
            expected_tools = cast(list[dict[str, Any]], server_spec["tools"])

            if self._dry_run:
                print(
                    f"[dry-run][mcp] ensure server {name} profile={profile} "
                    f"scope={scope} command={stdio_cfg['command']} tools={len(expected_tools)}"
                )
                continue

            client = self._client_for(owner_actor)
            servers = self._list_all_mcp_servers(client)
            existing = next((item for item in servers if item.get("name") == name), None)

            if existing is None:
                create_body: dict[str, Any] = {
                    "name": name,
                    "transport": "stdio",
                    "command": stdio_cfg["command"],
                    "args": stdio_cfg["args"],
                    "cwd": stdio_cfg["cwd"],
                    "secrets": {},
                    "visibility_scope": scope,
                }
                if scope == "department":
                    create_body["owner_department_id"] = owner_dept
                    create_body["visibility_grants"] = target_grants
                resp = self._require_status(
                    client.post("/api/v1/admin/mcp/servers", json=create_body),
                    201,
                    f"register_mcp:{name}",
                )
                existing = cast(dict[str, Any], resp.json())
                self.summary.created["mcp_servers"] += 1
            else:
                current_cfg = cast(dict[str, Any], existing.get("config", {}))
                cfg_changed = (
                    current_cfg.get("command") != stdio_cfg["command"]
                    or list(current_cfg.get("args", ())) != stdio_cfg["args"]
                    or current_cfg.get("cwd") != stdio_cfg["cwd"]
                )
                vis_changed = (
                    existing.get("visibility_scope") != scope
                    or existing.get("owner_department_id") != owner_dept
                    or (
                        scope == "department"
                        and _normalize_grants(existing.get("visibility_grants"))
                        != _normalize_grants(target_grants)
                    )
                    or not bool(existing.get("enabled", True))
                )
                if cfg_changed or vis_changed:
                    patch_body: dict[str, Any] = {
                        "expected_config_version": int(existing["config_version"]),
                        "config": {
                            "command": stdio_cfg["command"],
                            "args": stdio_cfg["args"],
                            "cwd": stdio_cfg["cwd"],
                        },
                        "visibility_scope": scope,
                        "enabled": True,
                        "rediscover": False,
                    }
                    if scope == "department":
                        patch_body["owner_department_id"] = owner_dept
                        patch_body["visibility_grants"] = target_grants
                    resp = self._require_status(
                        client.patch(
                            f"/api/v1/admin/mcp/servers/{existing['id']}",
                            json=patch_body,
                        ),
                        200,
                        f"patch_mcp:{name}",
                    )
                    existing = cast(dict[str, Any], resp.json())
                    # 配置变更后独立重发现远端工具，按 remote_name 无损保留 tool_id。
                    self._require_status(
                        client.post(f"/api/v1/admin/mcp/servers/{existing['id']}/rediscover"),
                        200,
                        f"rediscover_mcp:{name}",
                    )
                    self.summary.updated["mcp_servers"] += 1
                else:
                    self.summary.skipped["mcp_servers"] += 1

            server_id = str(existing["id"])
            server_tools = self._list_server_tools(client, server_id)
            expected_remote_names = {str(item["remote_name"]) for item in expected_tools}
            discovered_remote_names = {str(item["remote_name"]) for item in server_tools}
            if discovered_remote_names != expected_remote_names:
                self._require_status(
                    client.post(f"/api/v1/admin/mcp/servers/{server_id}/rediscover"),
                    200,
                    f"rediscover_mcp_missing:{name}",
                )
                server_tools = self._list_server_tools(client, server_id)

            tools_by_remote = {str(item["remote_name"]): item for item in server_tools}
            for tool_spec in expected_tools:
                remote_name = str(tool_spec["remote_name"])
                live_tool = tools_by_remote.get(remote_name)
                if live_tool is None:
                    raise RuntimeError(f"mcp_tool_missing server={name} remote_name={remote_name}")

                target_req_approval = bool(tool_spec["requires_approval"])
                target_display = tool_spec.get("display_name")
                target_template = tool_spec.get("approval_summary_template")
                target_labels = cast(dict[str, str], tool_spec.get("parameter_labels", {}))

                changed = (
                    bool(live_tool.get("requires_approval")) != target_req_approval
                    or live_tool.get("display_name") != target_display
                    or live_tool.get("approval_summary_template") != target_template
                    or dict(live_tool.get("parameter_labels") or {}) != target_labels
                )
                if not changed:
                    self.summary.skipped["mcp_tools"] += 1
                    continue

                tool_patch: dict[str, Any] = {
                    "requires_approval": target_req_approval,
                    "display_name": target_display,
                    "approval_summary_template": target_template,
                    "parameter_labels": target_labels,
                }
                self._require_status(
                    client.patch(
                        f"/api/v1/admin/tools/{live_tool['tool_id']}",
                        json=tool_patch,
                    ),
                    200,
                    f"patch_tool:{remote_name}",
                )
                self.summary.updated["mcp_tools"] += 1

    # ------------------------------------------------------------------
    # 4. 9 个双形态 Skills（在线 Markdown + ZIP 脚本 + 个人 Skill）幂等导入
    # ------------------------------------------------------------------

    def seed_skills(self) -> None:
        """幂等创建或更新 SK-1 至 SK-9 全部技能及推荐配置。"""
        for spec in cast(list[dict[str, Any]], self._manifest["skills"]):
            code = str(spec["code"])
            slug = str(spec["slug"])
            fmt = str(spec["format"])
            scope = str(spec["scope"])
            owner_actor = str(spec["owner_actor"])

            if self._dry_run:
                print(
                    f"[dry-run][skills] ensure {code} slug={slug} "
                    f"format={fmt} scope={scope} actor={owner_actor}"
                )
                continue

            if scope == "personal":
                self._seed_personal_skill(spec)
            elif fmt == "markdown":
                self._seed_admin_markdown_skill(spec)
            elif fmt == "package":
                self._seed_admin_package_skill(spec)
            else:
                raise ValueError(f"skill_format_unsupported:{fmt}")

    def _seed_personal_skill(self, spec: Mapping[str, Any]) -> None:
        """幂等导入个人 Markdown Skill（按名称复用，避免重复创建）。"""
        owner_actor = str(spec["owner_actor"])
        name = str(spec["name"])
        description = str(spec["description"])
        guide = (DATASET_ROOT / str(spec["source_file"])).read_text(encoding="utf-8")
        target_enabled = bool(spec.get("enabled", True))
        create_payload = {"name": name, "description": description, "guide": guide}
        digest = _canonical_json_sha256(create_payload)

        client = self._client_for(owner_actor)
        list_resp = self._require_status(
            client.get("/api/v1/me/skills"),
            200,
            f"list_personal_skills:{owner_actor}",
        )
        items = cast(list[dict[str, Any]], list_resp.json().get("items", []))
        existing = next((item for item in items if item.get("name") == name), None)

        if existing is None:
            self._require_status(
                client.post(
                    "/api/v1/me/skills",
                    headers={
                        "Idempotency-Key": _make_idempotency_key(
                            "seed-pskill", str(spec["code"]), digest[:20]
                        )
                    },
                    json=create_payload,
                ),
                201,
                f"create_personal_skill:{spec['code']}",
            )
            self.summary.created["skills"] += 1
            return

        detail_resp = self._require_status(
            client.get(f"/api/v1/me/skills/{existing['id']}"),
            200,
            f"get_personal_skill:{spec['code']}",
        )
        detail = cast(dict[str, Any], detail_resp.json())
        versions = cast(list[dict[str, Any]], detail.get("versions", []))
        current_ver = next(
            (v for v in versions if v.get("id") == detail.get("current_version_id")),
            versions[0] if versions else {},
        )
        revision = int(detail["revision"])
        mutated = False
        if current_ver.get("description") != description or current_ver.get("guide") != guide:
            # 将当前 expected_revision 编入发布幂等键，避免反复修改并恢复基线时触发 409 冲突。
            pub_resp = self._require_status(
                client.post(
                    f"/api/v1/me/skills/{existing['id']}/versions",
                    headers={
                        "Idempotency-Key": _make_idempotency_key(
                            "seed-pskill-ver", str(spec["code"]), f"r{revision}", digest[:16]
                        )
                    },
                    json={
                        "name": name,
                        "description": description,
                        "guide": guide,
                        "expected_revision": revision,
                    },
                ),
                201,
                f"publish_personal_skill:{spec['code']}",
            )
            revision = int(pub_resp.json()["revision"])
            mutated = True

        if bool(detail.get("enabled", True)) != target_enabled:
            self._require_status(
                client.patch(
                    f"/api/v1/me/skills/{existing['id']}",
                    json={"enabled": target_enabled, "expected_revision": revision},
                ),
                200,
                f"toggle_personal_skill:{spec['code']}",
            )
            mutated = True

        if mutated:
            self.summary.updated["skills"] += 1
        else:
            self.summary.skipped["skills"] += 1

    def _seed_admin_markdown_skill(self, spec: Mapping[str, Any]) -> None:
        """幂等导入公司级或部门级在线 Markdown 技能（按 slug 复用）。"""
        owner_actor = str(spec["owner_actor"])
        slug = str(spec["slug"])
        name = str(spec["name"])
        description = str(spec["description"])
        guide = (DATASET_ROOT / str(spec["source_file"])).read_text(encoding="utf-8")
        scope = str(spec["scope"])
        owner_dept = spec.get("owner_department_id")
        target_grants = cast(list[dict[str, Any]], spec.get("visibility_grants", []))
        target_enabled = bool(spec.get("enabled", True))
        target_featured = bool(spec.get("featured", False))
        target_starter = spec.get("starter_prompt")
        ver_digest = _canonical_json_sha256(
            {"name": name, "description": description, "guide": guide}
        )

        client = self._client_for(owner_actor)
        list_resp = self._require_status(
            client.get("/api/v1/admin/skills"),
            200,
            f"list_admin_skills:{owner_actor}",
        )
        items = cast(list[dict[str, Any]], list_resp.json().get("items", []))
        existing = next((item for item in items if item.get("slug") == slug), None)

        if existing is None:
            body: dict[str, Any] = {
                "name": name,
                "description": description,
                "guide": guide,
                "scope": scope,
                "slug": slug,
                "enabled": target_enabled,
                "featured": target_featured,
                "starter_prompt": target_starter,
            }
            if scope == "department":
                body["owner_department_id"] = owner_dept
                body["visibility_grants"] = target_grants
            # 基于完整创建请求体计算指纹，确保 scope/grants/featured/starter_prompt 变化不碰撞旧键。
            create_digest = _canonical_json_sha256(body)
            self._require_status(
                client.post(
                    "/api/v1/admin/skills/markdown",
                    headers={
                        "Idempotency-Key": _make_idempotency_key(
                            "seed-mdskill", slug[:40], create_digest[:20]
                        )
                    },
                    json=body,
                ),
                201,
                f"create_admin_markdown_skill:{slug}",
            )
            self.summary.created["skills"] += 1
            return

        detail_resp = self._require_status(
            client.get(f"/api/v1/admin/skills/{existing['id']}"),
            200,
            f"get_admin_skill:{slug}",
        )
        detail = cast(dict[str, Any], detail_resp.json())
        versions = cast(list[dict[str, Any]], detail.get("versions", []))
        current_ver = next(
            (v for v in versions if v.get("id") == detail.get("current_version_id")),
            versions[0] if versions else {},
        )
        mutated = False
        if (
            current_ver.get("name") != name
            or current_ver.get("description") != description
            or current_ver.get("guide") != guide
        ):
            expected_rev = int(detail["revision"])
            # 将当前 expected_revision 编入发布幂等键，避免反复修改后恢复基线触发 409 冲突。
            pub_resp = self._require_status(
                client.post(
                    f"/api/v1/admin/skills/{existing['id']}/versions/markdown",
                    headers={
                        "Idempotency-Key": _make_idempotency_key(
                            "seed-mdskill-ver", slug[:40], f"r{expected_rev}", ver_digest[:16]
                        )
                    },
                    json={
                        "name": name,
                        "description": description,
                        "guide": guide,
                        "expected_revision": expected_rev,
                        "activate": True,
                    },
                ),
                201,
                f"publish_admin_markdown_skill:{slug}",
            )
            detail = cast(dict[str, Any], pub_resp.json())
            mutated = True

        grants_diff = scope == "department" and _normalize_grants(
            detail.get("visibility_grants")
        ) != _normalize_grants(target_grants)
        config_diff = (
            bool(detail.get("enabled")) != target_enabled
            or bool(detail.get("featured")) != target_featured
            or detail.get("starter_prompt") != target_starter
            or grants_diff
        )
        if config_diff:
            patch_body: dict[str, Any] = {
                "expected_revision": int(detail["revision"]),
                "current_version_id": str(detail["current_version_id"]),
                "enabled": target_enabled,
                "script_requires_approval": bool(spec.get("script_requires_approval", False)),
                "featured": target_featured,
                "starter_prompt": target_starter,
            }
            if scope == "department":
                patch_body["visibility_grants"] = target_grants
            self._require_status(
                client.patch(f"/api/v1/admin/skills/{existing['id']}", json=patch_body),
                200,
                f"configure_admin_markdown_skill:{slug}",
            )
            mutated = True

        if mutated:
            self.summary.updated["skills"] += 1
        else:
            self.summary.skipped["skills"] += 1

    def _seed_admin_package_skill(self, spec: Mapping[str, Any]) -> None:
        """幂等导入 ZIP 脚本技能包（如 SK-5、SK-7）并确保启用、脚本审批与推荐配置一致。"""
        owner_actor = str(spec["owner_actor"])
        slug = str(spec["slug"])
        scope = str(spec["scope"])
        owner_dept = spec.get("owner_department_id")
        target_grants = cast(list[dict[str, Any]], spec.get("visibility_grants", []))
        target_enabled = bool(spec.get("enabled", True))
        target_approval = bool(spec.get("script_requires_approval", True))
        target_featured = bool(spec.get("featured", True))
        target_starter = spec.get("starter_prompt")

        pkg_path = DATASET_ROOT / str(spec["package_file"])
        if not pkg_path.is_file():
            raise FileNotFoundError(
                f"skill_zip_missing:{pkg_path} (请先运行 build_assets.py 或加 --rebuild-assets)"
            )
        pkg_bytes = pkg_path.read_bytes()
        pkg_sha = _sha256_bytes(pkg_bytes)

        client = self._client_for(owner_actor)
        list_resp = self._require_status(
            client.get("/api/v1/admin/skills"),
            200,
            f"list_admin_skills:{owner_actor}",
        )
        items = cast(list[dict[str, Any]], list_resp.json().get("items", []))
        existing = next((item for item in items if item.get("slug") == slug), None)

        if existing is None:
            form_data: dict[str, str] = {"scope": scope}
            if scope == "department" and isinstance(owner_dept, str):
                form_data["owner_department_id"] = owner_dept
                form_data["visibility_grants"] = json.dumps(target_grants, ensure_ascii=False)
            import_digest = _canonical_json_sha256(
                {
                    "slug": slug,
                    "scope": scope,
                    "owner_department_id": owner_dept,
                    "visibility_grants": target_grants,
                    "enabled": target_enabled,
                    "script_requires_approval": target_approval,
                    "featured": target_featured,
                    "starter_prompt": target_starter,
                    "package_sha256": pkg_sha,
                }
            )
            imp_resp = self._require_status(
                client.post(
                    "/api/v1/admin/skills/import",
                    headers={
                        "Idempotency-Key": _make_idempotency_key(
                            "seed-pkgskill", slug[:40], import_digest[:20]
                        )
                    },
                    data=form_data,
                    files={"package": (pkg_path.name, pkg_bytes, "application/zip")},
                ),
                201,
                f"import_zip_skill:{slug}",
            )
            detail = cast(dict[str, Any], imp_resp.json())
            versions = cast(list[dict[str, Any]], detail.get("versions", []))
            target_version_id = str(detail.get("current_version_id") or versions[0]["id"])
            patch_body: dict[str, Any] = {
                "expected_revision": int(detail["revision"]),
                "current_version_id": target_version_id,
                "enabled": target_enabled,
                "script_requires_approval": target_approval,
                "featured": target_featured,
                "starter_prompt": target_starter,
            }
            if scope == "department":
                patch_body["visibility_grants"] = target_grants
            self._require_status(
                client.patch(f"/api/v1/admin/skills/{detail['id']}", json=patch_body),
                200,
                f"configure_zip_skill:{slug}",
            )
            self.summary.created["skills"] += 1
            return

        detail_resp = self._require_status(
            client.get(f"/api/v1/admin/skills/{existing['id']}"),
            200,
            f"get_zip_skill:{slug}",
        )
        detail = cast(dict[str, Any], detail_resp.json())
        versions = cast(list[dict[str, Any]], detail.get("versions", []))
        current_ver = next(
            (v for v in versions if v.get("id") == detail.get("current_version_id")),
            versions[0] if versions else {},
        )
        target_version_id = str(detail.get("current_version_id") or current_ver.get("id", ""))
        mutated = False

        if current_ver.get("package_sha256") != pkg_sha:
            expected_rev = int(detail["revision"])
            ver_tag = _sha256_bytes(str(target_version_id or "none").encode("utf-8"))[:10]
            ver_resp = self._require_status(
                client.post(
                    f"/api/v1/admin/skills/{existing['id']}/versions",
                    headers={
                        "Idempotency-Key": _make_idempotency_key(
                            "seed-pkgskill-ver",
                            slug[:40],
                            f"r{expected_rev}",
                            ver_tag,
                            pkg_sha[:16],
                        )
                    },
                    files={"package": (pkg_path.name, pkg_bytes, "application/zip")},
                ),
                201,
                f"add_zip_skill_version:{slug}",
            )
            target_version_id = str(ver_resp.json()["id"])
            mutated = True

        grants_diff = scope == "department" and _normalize_grants(
            detail.get("visibility_grants")
        ) != _normalize_grants(target_grants)
        config_diff = (
            mutated
            or bool(detail.get("enabled")) != target_enabled
            or bool(detail.get("script_requires_approval")) != target_approval
            or bool(detail.get("featured")) != target_featured
            or detail.get("starter_prompt") != target_starter
            or grants_diff
        )
        if config_diff:
            patch_body = {
                "expected_revision": int(detail["revision"]),
                "current_version_id": target_version_id,
                "enabled": target_enabled,
                "script_requires_approval": target_approval,
                "featured": target_featured,
                "starter_prompt": target_starter,
            }
            if scope == "department":
                patch_body["visibility_grants"] = target_grants
            self._require_status(
                client.patch(f"/api/v1/admin/skills/{existing['id']}", json=patch_body),
                200,
                f"configure_zip_skill:{slug}",
            )
            self.summary.updated["skills"] += 1
        else:
            self.summary.skipped["skills"] += 1

    # ------------------------------------------------------------------
    # 5. 3 个部门/公司 Subagent 幂等导入（动态解析真实 tool_id + 乐观锁 PATCH）
    # ------------------------------------------------------------------

    def _list_all_subagents(self, client: httpx.Client) -> list[dict[str, Any]]:
        """按 offset/limit 分页读取管理员可见的全部 Subagent 配置。"""
        items: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            resp = self._require_status(
                client.get(
                    "/api/v1/admin/subagents",
                    params={"offset": offset, "limit": limit},
                ),
                200,
                "list_subagents",
            )
            payload = cast(dict[str, Any], resp.json())
            batch = cast(list[dict[str, Any]], payload.get("items", []))
            items.extend(batch)
            total = int(payload.get("total", len(items)))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return items

    def _resolve_subagent_tool_catalog(
        self, client: httpx.Client
    ) -> tuple[set[str], dict[str, str]]:
        """聚合 available-tools 与已注册 MCP 工具表，映射工具名至真实 tool_id。"""
        avail_resp = self._require_status(
            client.get("/api/v1/admin/subagents/available-tools"),
            200,
            "list_subagent_available_tools",
        )
        available_tool_ids: set[str] = set()
        name_to_tool_id: dict[str, str] = {}
        for group in cast(list[dict[str, Any]], avail_resp.json().get("groups", [])):
            for item in cast(list[dict[str, Any]], group.get("items", [])):
                tool_id = str(item["tool_id"])
                tool_name = str(item["name"])
                available_tool_ids.add(tool_id)
                name_to_tool_id[tool_id] = tool_id
                name_to_tool_id[tool_name] = tool_id

        # 真实后端中 MCP 工具的 model_name 为 mcp__<uuid>，需再从 MCP Server 按 remote_name 对齐。
        for server in self._list_all_mcp_servers(client):
            server_id = str(server["id"])
            for tool in self._list_server_tools(client, server_id):
                tid = str(tool["tool_id"])
                remote_name = str(tool["remote_name"])
                if tid in available_tool_ids:
                    name_to_tool_id[remote_name] = tid

        return available_tool_ids, name_to_tool_id

    def _resolve_allowed_tool_ids(
        self,
        tool_names: Sequence[str],
        *,
        available_tool_ids: set[str],
        name_to_tool_id: dict[str, str],
        subagent_slug: str,
    ) -> list[str]:
        """将 manifest 声明的内置/MCP 工具名解析为后端真实且可用的 tool_id 列表。"""
        resolved: list[str] = []
        for raw_name in tool_names:
            name = str(raw_name).strip()
            tool_id = name_to_tool_id.get(name)
            if tool_id is None or tool_id not in available_tool_ids:
                raise RuntimeError(f"subagent_tool_not_found slug={subagent_slug} tool={name!r}")
            if tool_id not in resolved:
                resolved.append(tool_id)
        return resolved

    def seed_subagents(self) -> None:
        """幂等创建或更新 SUB-1 至 SUB-3 Subagent 配置（含工具白名单与乐观锁对齐）。"""
        subagent_specs = cast(list[dict[str, Any]], self._manifest.get("subagents", []))
        if not subagent_specs:
            return

        if self._dry_run:
            for spec in subagent_specs:
                tool_count = len(cast(Sequence[Any], spec.get("allowed_tool_names", ())))
                print(
                    f"[dry-run][subagents] ensure {spec['code']} slug={spec['slug']} "
                    f"scope={spec['visibility_scope']} tools={tool_count}"
                )
            return

        # Subagent 管理路由强制要求 super_admin 身份。
        admin_client = self._client_for("mock_super_admin")
        available_tool_ids, name_to_tool_id = self._resolve_subagent_tool_catalog(admin_client)
        existing_by_slug = {
            str(item["slug"]): item for item in self._list_all_subagents(admin_client)
        }

        for spec in subagent_specs:
            slug = str(spec["slug"])
            name = str(spec["name"])
            description = str(spec["description"]).strip()
            system_prompt = str(spec["system_prompt"]).strip()
            scope = str(spec["visibility_scope"])
            owner_dept = spec.get("owner_department_id")
            target_grants = cast(list[dict[str, Any]], spec.get("visibility_grants", []))
            model_profile_id = spec.get("model_profile_id")
            target_enabled = bool(spec.get("enabled", True))
            max_llm_requests = int(spec["max_llm_requests"])
            timeout_seconds = int(spec["timeout_seconds"])
            resolved_tool_ids = self._resolve_allowed_tool_ids(
                cast(Sequence[str], spec.get("allowed_tool_names", ())),
                available_tool_ids=available_tool_ids,
                name_to_tool_id=name_to_tool_id,
                subagent_slug=slug,
            )

            existing = existing_by_slug.get(slug)
            if existing is None:
                # 严格遵守 SubagentCreateRequest(extra="forbid", strict=True) 契约。
                create_body: dict[str, Any] = {
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "system_prompt": system_prompt,
                    "model_profile_id": model_profile_id,
                    "allowed_tool_ids": resolved_tool_ids,
                    "enabled": target_enabled,
                    "max_llm_requests": max_llm_requests,
                    "timeout_seconds": timeout_seconds,
                    "visibility_scope": scope,
                }
                if scope == "department":
                    create_body["owner_department_id"] = owner_dept
                    create_body["visibility_grants"] = target_grants
                resp = self._require_status(
                    admin_client.post("/api/v1/admin/subagents", json=create_body),
                    201,
                    f"create_subagent:{slug}",
                )
                created = cast(dict[str, Any], resp.json())
                self.summary.created["subagents"] += 1
                self.summary.subagent_ids_by_slug[slug] = str(created["id"])
                continue

            subagent_id = str(existing["id"])
            self.summary.subagent_ids_by_slug[slug] = subagent_id
            grants_diff = scope == "department" and _normalize_grants(
                existing.get("visibility_grants")
            ) != _normalize_grants(target_grants)
            expected_owner_dept = owner_dept if scope == "department" else None
            body_diff = (
                existing.get("name") != name
                or existing.get("description") != description
                or existing.get("system_prompt") != system_prompt
                or existing.get("model_profile_id") != model_profile_id
                or list(existing.get("allowed_tool_ids") or ()) != resolved_tool_ids
                or existing.get("max_llm_requests") != max_llm_requests
                or existing.get("timeout_seconds") != timeout_seconds
                or existing.get("visibility_scope") != scope
                or existing.get("owner_department_id") != expected_owner_dept
                or grants_diff
            )
            enabled_diff = bool(existing.get("enabled", True)) != target_enabled

            mutated = False
            if body_diff:
                patch_body: dict[str, Any] = {
                    "version": int(existing["version"]),
                    "name": name,
                    "description": description,
                    "system_prompt": system_prompt,
                    "model_profile_id": model_profile_id,
                    "allowed_tool_ids": resolved_tool_ids,
                    "max_llm_requests": max_llm_requests,
                    "timeout_seconds": timeout_seconds,
                    "visibility_scope": scope,
                }
                if scope == "department":
                    patch_body["owner_department_id"] = owner_dept
                    patch_body["visibility_grants"] = target_grants
                else:
                    patch_body["owner_department_id"] = None
                    patch_body["visibility_grants"] = None
                self._require_status(
                    admin_client.patch(f"/api/v1/admin/subagents/{subagent_id}", json=patch_body),
                    200,
                    f"patch_subagent:{slug}",
                )
                mutated = True

            if enabled_diff:
                self._require_status(
                    admin_client.put(
                        f"/api/v1/admin/subagents/{subagent_id}/enabled",
                        json={"enabled": target_enabled},
                    ),
                    200,
                    f"toggle_subagent:{slug}",
                )
                mutated = True

            if mutated:
                self.summary.updated["subagents"] += 1
            else:
                self.summary.skipped["subagents"] += 1

    # ------------------------------------------------------------------
    # 6. 2 个业务专项 Project 及其默认技能/知识库绑定幂等导入
    # ------------------------------------------------------------------

    def _list_all_projects(self, client: httpx.Client) -> list[dict[str, Any]]:
        """分页读取当前角色名下全部未删除项目。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._require_status(
                client.get("/api/v1/projects", params={"page": page, "page_size": 100}),
                200,
                "list_projects",
            )
            batch = cast(list[dict[str, Any]], resp.json().get("items", []))
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def _resolve_project_skill_ids(
        self,
        client: httpx.Client,
        skill_codes: Sequence[str],
        project_code: str,
    ) -> list[str]:
        """按 owner 可见技能目录，将 manifest 中的技能代码（如 SK-5）解析为真实 skill_id。"""
        code_to_slug = {
            str(item["code"]): str(item["slug"])
            for item in cast(list[dict[str, Any]], self._manifest.get("skills", []))
        }
        skills_resp = self._require_status(
            client.get("/api/v1/skills"),
            200,
            f"list_skills_for_project:{project_code}",
        )
        slug_to_id = {
            str(item["slug"]): str(item["id"])
            for item in cast(list[dict[str, Any]], skills_resp.json().get("items", []))
        }
        resolved: list[str] = []
        for code in skill_codes:
            slug = code_to_slug.get(str(code))
            if slug is None or slug not in slug_to_id:
                raise RuntimeError(
                    f"project_default_skill_not_found project={project_code} code={code}"
                )
            resolved.append(slug_to_id[slug])
        return resolved

    def _resolve_project_kb_ids(
        self,
        client: httpx.Client,
        kb_keys: Sequence[str],
        project_code: str,
    ) -> list[str]:
        """按 owner 可见知识库列表，将 manifest 中的 KB key 解析为真实 base_id。"""
        kb_specs_by_key = {
            str(item["key"]): item
            for item in cast(list[dict[str, Any]], self._manifest.get("knowledge_bases", []))
        }
        visible_bases = self._list_all_bases(client)
        resolved: list[str] = []
        for kb_key in kb_keys:
            spec = kb_specs_by_key.get(str(kb_key))
            if spec is None:
                raise RuntimeError(f"project_kb_key_unknown project={project_code} kb={kb_key}")
            matched = next(
                (
                    base
                    for base in visible_bases
                    if base.get("name") == spec["name"]
                    and base.get("visibility_scope") == spec["visibility_scope"]
                    and base.get("owner_department_id") == spec.get("owner_department_id")
                ),
                None,
            )
            if matched is None:
                raise RuntimeError(f"project_kb_not_accessible project={project_code} kb={kb_key}")
            resolved.append(str(matched["id"]))
        return resolved

    def seed_projects(self) -> None:
        """幂等创建或更新 PROJ-1 / PROJ-2 专项项目并绑定其默认技能与知识库。"""
        project_specs = cast(list[dict[str, Any]], self._manifest.get("projects", []))
        if not project_specs:
            return

        for spec in project_specs:
            code = str(spec["code"])
            owner_actor = str(spec["owner_actor"])
            name = str(spec["name"]).strip()
            description = str(spec["description"]).strip()
            instructions = str(spec["instructions"]).strip()
            skill_codes = cast(list[str], spec.get("default_skill_codes", []))
            kb_keys = cast(list[str], spec.get("knowledge_base_keys", []))

            if self._dry_run:
                print(
                    f"[dry-run][projects] ensure {code} ({name}) actor={owner_actor} "
                    f"skills={skill_codes} kbs={kb_keys}"
                )
                continue

            client = self._client_for(owner_actor)
            skill_ids = self._resolve_project_skill_ids(client, skill_codes, code)
            kb_ids = self._resolve_project_kb_ids(client, kb_keys, code)

            existing_projects = self._list_all_projects(client)
            matched = next((item for item in existing_projects if item.get("name") == name), None)

            if matched is None:
                resp = self._require_status(
                    client.post(
                        "/api/v1/projects",
                        json={
                            "name": name,
                            "description": description,
                            "instructions": instructions,
                            "default_skill_ids": skill_ids,
                        },
                    ),
                    201,
                    f"create_project:{code}",
                )
                matched = cast(dict[str, Any], resp.json())
                self.summary.created["projects"] += 1
            else:
                # 仅在简介、背景指令或默认技能漂移时局部更新，保持幂等零多余写请求。
                needs_patch = (
                    matched.get("description") != description
                    or matched.get("instructions") != instructions
                    or list(matched.get("default_skill_ids") or ()) != skill_ids
                )
                if needs_patch:
                    resp = self._require_status(
                        client.patch(
                            f"/api/v1/projects/{matched['id']}",
                            json={
                                "description": description,
                                "instructions": instructions,
                                "default_skill_ids": skill_ids,
                            },
                        ),
                        200,
                        f"patch_project:{code}",
                    )
                    matched = cast(dict[str, Any], resp.json())
                    self.summary.updated["projects"] += 1
                else:
                    self.summary.skipped["projects"] += 1

            project_id = str(matched["id"])
            self.summary.project_ids_by_code[code] = project_id

            bound_resp = self._require_status(
                client.get(f"/api/v1/projects/{project_id}/knowledge-bases"),
                200,
                f"list_project_kbs:{code}",
            )
            bound_ids = {str(item["id"]) for item in cast(list[dict[str, Any]], bound_resp.json())}
            for base_id in kb_ids:
                if base_id in bound_ids:
                    self.summary.skipped["project_kb_bindings"] += 1
                    continue
                self._require_status(
                    client.put(f"/api/v1/projects/{project_id}/knowledge-bases/{base_id}"),
                    204,
                    f"bind_project_kb:{code}:{base_id}",
                )
                self.summary.created["project_kb_bindings"] += 1

    # ------------------------------------------------------------------
    # 7. 在线多角色验收（--verify，覆盖资产/RBAC、110 题量化评测及可选组织停用演练）
    # ------------------------------------------------------------------

    def verify_all(self) -> None:
        """执行端到端在线验收：KB、RBAC、MCP、Skills、Subagents、Projects 与 110 题量化检索。"""
        if self._dry_run:
            print("[dry-run][verify] skip live HTTP assertions")
            return

        # 7.1 定位 6 个知识库并检查 36 份文档就绪状态（DOC-06/DOC-29 OCR 失败不阻断其余 34 档）。
        kb_ids: dict[str, str] = {}
        doc_ids: dict[str, str] = {}
        for kb_spec in cast(list[dict[str, Any]], self._manifest["knowledge_bases"]):
            kb_key = str(kb_spec["key"])
            client = self._client_for(str(kb_spec["owner_actor"]))
            bases = self._list_all_bases(client)
            matched = next(
                (
                    b
                    for b in bases
                    if b.get("name") == kb_spec["name"]
                    and b.get("visibility_scope") == kb_spec["visibility_scope"]
                    and b.get("owner_department_id") == kb_spec.get("owner_department_id")
                ),
                None,
            )
            if matched is None:
                raise AssertionError(f"verify_kb_missing:{kb_key}")
            kb_id = str(matched["id"])
            kb_ids[kb_key] = kb_id

            docs = self._list_all_documents(client, kb_id)
            by_title = {str(d["title"]): d for d in docs}
            for doc_spec in cast(list[dict[str, Any]], kb_spec["documents"]):
                doc_code = str(doc_spec["doc_id"])
                requires_ocr = bool(doc_spec.get("requires_ocr", False))
                found = by_title.get(str(doc_spec["title"]))
                if found is None:
                    if requires_ocr and self._skip_kb_ocr:
                        continue
                    raise AssertionError(f"verify_doc_missing:{doc_code}")
                doc_ids[doc_code] = str(found["id"])
                latest = cast(dict[str, Any] | None, found.get("latest_version"))
                state = str(latest.get("state")) if latest else "ready"
                if state != "ready":
                    if requires_ocr:
                        self.summary.warnings.append(f"verify_ocr_doc_not_ready:{doc_code}:{state}")
                    else:
                        raise AssertionError(f"verify_doc_not_ready:{doc_code}:{state}")
        self.summary.verified_checks.append("knowledge_bases_and_documents_ready")

        # 7.2 三层权限与双个人库（KB-5/KB-6）交叉 403/404 矩阵验收。
        super_client = self._client_for("mock_super_admin")
        fin_admin_client = self._client_for("mock_fin_admin")
        rd_member_client = self._client_for("mock_rd_member")
        hr_member_client = self._client_for("mock_hr_member")
        fin_member_client = self._client_for("mock_fin_member")

        # 双个人知识库交叉隔离 + 超管 404 防探测。
        assert (
            super_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_su_personal_notes']}"
            ).status_code
            == 404
        ), "super_admin_must_get_404_on_kb5"
        assert (
            super_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_lu_personal_notes']}"
            ).status_code
            == 404
        ), "super_admin_must_get_404_on_kb6"
        assert (
            hr_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_lu_personal_notes']}"
            ).status_code
            == 404
        ), "hr_member_must_get_404_on_kb6"
        assert (
            rd_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_su_personal_notes']}"
            ).status_code
            == 404
        ), "rd_member_must_get_404_on_kb5"
        assert (
            rd_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_lu_personal_notes']}"
            ).status_code
            == 200
        ), "rd_member_must_read_own_kb6"

        assert (
            rd_member_client.get(f"/api/v1/knowledge-bases/{kb_ids['kb_hr_ops']}").status_code
            == 404
        ), "rd_member_must_get_404_on_hr_kb"
        assert (
            rd_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_finance_collab']}"
            ).status_code
            == 404
        ), "rd_member_must_get_404_on_fin_kb"
        assert (
            hr_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_finance_collab']}"
            ).status_code
            == 200
        ), "hr_member_must_read_granted_fin_kb"
        assert (
            rd_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_rd_engineering']}"
            ).status_code
            == 200
        ), "rd_subdept_member_must_read_rd_kb"
        assert (
            fin_member_client.get(
                f"/api/v1/knowledge-bases/{kb_ids['kb_rd_engineering']}"
            ).status_code
            == 404
        ), "fin_member_must_get_404_on_rd_kb"
        # 财务部门管理员尝试把 KB-3 越权授权给非管辖部门 dept_mock_hr -> 必须返回 403。
        cross_grant_resp = fin_admin_client.patch(
            f"/api/v1/knowledge-bases/{kb_ids['kb_finance_collab']}",
            json={
                "visibility_grants": [
                    {
                        "target_type": "department",
                        "target_id": "dept_mock_fin",
                        "include_subdepartments": True,
                    },
                    {
                        "target_type": "department",
                        "target_id": "dept_mock_hr",
                        "include_subdepartments": True,
                    },
                ]
            },
        )
        assert cross_grant_resp.status_code == 403, (
            f"fin_admin_cross_dept_grant_must_be_403 got={cross_grant_resp.status_code}"
        )
        self.summary.verified_checks.append("rbac_403_404_matrix")

        # 7.3 3 套 MCP（16 工具）可见性隔离与全部 9 个写入工具中文审批元信息验收。
        servers = self._list_all_mcp_servers(super_client)
        servers_by_name = {str(s["name"]): s for s in servers}
        assert {"hroa-standard", "hroa-hr-ops", "hroa-fin-ops"} <= set(servers_by_name), (
            "hroa_mcp_servers_missing"
        )
        std_tools = self._list_server_tools(
            super_client, str(servers_by_name["hroa-standard"]["id"])
        )
        hr_tools = self._list_server_tools(super_client, str(servers_by_name["hroa-hr-ops"]["id"]))
        fin_tools = self._list_server_tools(
            super_client, str(servers_by_name["hroa-fin-ops"]["id"])
        )
        all_admin_tools = {str(t["remote_name"]): t for t in [*std_tools, *hr_tools, *fin_tools]}
        assert len(std_tools) == 9 and len(hr_tools) == 5 and len(fin_tools) == 2, (
            "hroa_mcp_tool_counts_mismatch"
        )

        write_remote_names = {
            "hroa_submit_leave_request",
            "hroa_submit_transfer_request",
            "hroa_submit_contract_seal_request",
            "hroa_submit_it_asset_request",
            "hroa_initiate_onboarding",
            "hroa_initiate_offboarding",
            "hroa_update_clearance_item",
            "hroa_submit_performance_calibration",
            "hroa_flag_expense_audit_result",
        }
        for remote_name in write_remote_names:
            tool = all_admin_tools[remote_name]
            assert tool.get("requires_approval") is True, f"write_tool_approval_off:{remote_name}"
            assert tool.get("display_name"), f"write_tool_display_name_empty:{remote_name}"
            assert tool.get("approval_summary_template"), f"write_tool_template_empty:{remote_name}"
            assert tool.get("parameter_labels"), f"write_tool_labels_empty:{remote_name}"

        std_tool_ids = {str(t["tool_id"]) for t in std_tools}
        hr_tool_ids = {str(t["tool_id"]) for t in hr_tools}
        fin_tool_ids = {str(t["tool_id"]) for t in fin_tools}
        rd_public_ids = {str(t["tool_id"]) for t in self._list_all_public_tools(rd_member_client)}
        hr_public_ids = {str(t["tool_id"]) for t in self._list_all_public_tools(hr_member_client)}
        fin_public_ids = {str(t["tool_id"]) for t in self._list_all_public_tools(fin_member_client)}
        super_public_ids = {str(t["tool_id"]) for t in self._list_all_public_tools(super_client)}
        assert std_tool_ids <= rd_public_ids, "rd_member_must_see_standard_tools"
        assert not (hr_tool_ids & rd_public_ids), "rd_member_must_not_see_hr_ops_tools"
        assert not (fin_tool_ids & rd_public_ids), "rd_member_must_not_see_fin_ops_tools"
        assert (std_tool_ids | hr_tool_ids) <= hr_public_ids, "hr_member_must_see_std_and_hr_tools"
        assert not (fin_tool_ids & hr_public_ids), "hr_member_must_not_see_fin_ops_tools"
        assert (std_tool_ids | fin_tool_ids) <= fin_public_ids, (
            "fin_member_must_see_std_and_fin_tools"
        )
        assert not (hr_tool_ids & fin_public_ids), "fin_member_must_not_see_hr_ops_tools"
        assert (std_tool_ids | hr_tool_ids | fin_tool_ids) <= super_public_ids, (
            "super_admin_must_see_all_mcp_tools"
        )
        self.summary.verified_checks.append("mcp_visibility_and_approval_metadata")

        # 7.4 9 项 Skills 可见性隔离（含 SK-7 仅财务与超管可见）及 featured 排序验收。
        hr_skills = cast(
            list[dict[str, Any]],
            self._require_status(hr_member_client.get("/api/v1/skills"), 200, "verify_hr_skills")
            .json()
            .get("items", []),
        )
        hr_slugs = {str(item["slug"]) for item in hr_skills}
        assert {
            "hr-leave-assistant",
            "hr-onboarding-guide",
            "hr-offboarding-audit",
            "hroa-lifecycle-audit-pack",
            "contract-seal-review-guide",
            "performance-review-assistant",
        } <= hr_slugs, "hr_member_missing_expected_skills"
        assert "rd-team-handover-guide" not in hr_slugs, "hr_member_must_not_see_rd_skill"
        assert "travel-expense-audit-pack" not in hr_slugs, "hr_member_must_not_see_sk7"
        assert any(
            item.get("scope") == "personal" and item.get("name") == "苏晚晴 HRBP 周报整理助手"
            for item in hr_skills
        ), "hr_member_missing_personal_skill"
        # 验证首页推荐技能严格排在非推荐技能之前。
        seen_non_featured = False
        for item in hr_skills:
            if not bool(item.get("featured")):
                seen_non_featured = True
            elif seen_non_featured:
                raise AssertionError("skills_featured_order_violated")

        rd_slugs = {
            str(item["slug"])
            for item in self._require_status(
                rd_member_client.get("/api/v1/skills"), 200, "verify_rd_skills"
            )
            .json()
            .get("items", [])
        }
        assert {
            "hr-leave-assistant",
            "rd-team-handover-guide",
            "contract-seal-review-guide",
            "performance-review-assistant",
        } <= rd_slugs
        assert not (
            {"hr-onboarding-guide", "hroa-lifecycle-audit-pack", "travel-expense-audit-pack"}
            & rd_slugs
        )

        fin_slugs = {
            str(item["slug"])
            for item in self._require_status(
                fin_member_client.get("/api/v1/skills"), 200, "verify_fin_skills"
            )
            .json()
            .get("items", [])
        }
        assert {
            "hr-leave-assistant",
            "hr-offboarding-audit",
            "travel-expense-audit-pack",
            "contract-seal-review-guide",
            "performance-review-assistant",
        } <= fin_slugs, "fin_member_missing_sk7_or_shared_skills"
        assert not ({"hr-onboarding-guide", "rd-team-handover-guide"} & fin_slugs)

        super_slugs = {
            str(item["slug"])
            for item in self._require_status(
                super_client.get("/api/v1/skills"), 200, "verify_super_skills"
            )
            .json()
            .get("items", [])
        }
        assert "travel-expense-audit-pack" in super_slugs, "super_admin_must_see_sk7"
        self.summary.verified_checks.append("skills_visibility_and_featured_order")

        # 7.5 3 个 Subagents 列表、可见范围、工具白名单及非超管 403 隔离验收。
        assert fin_admin_client.get("/api/v1/admin/subagents").status_code == 403, (
            "fin_admin_must_get_403_on_subagent_admin"
        )
        assert rd_member_client.get("/api/v1/admin/subagents").status_code == 403, (
            "rd_member_must_get_403_on_subagent_admin"
        )
        available_tool_ids, name_to_tool_id = self._resolve_subagent_tool_catalog(super_client)
        live_subagents = {
            str(item["slug"]): item for item in self._list_all_subagents(super_client)
        }
        for spec in cast(list[dict[str, Any]], self._manifest.get("subagents", [])):
            slug = str(spec["slug"])
            live = live_subagents.get(slug)
            assert live is not None, f"verify_subagent_missing:{slug}"
            assert bool(live.get("enabled")) is bool(spec.get("enabled", True))
            assert live.get("model_profile_id") == spec.get("model_profile_id")
            assert live.get("max_llm_requests") == spec.get("max_llm_requests")
            assert live.get("timeout_seconds") == spec.get("timeout_seconds")
            expected_scope = str(spec["visibility_scope"])
            expected_dept = (
                spec.get("owner_department_id") if expected_scope == "department" else None
            )
            expected_grants = (
                cast(list[dict[str, Any]], spec.get("visibility_grants", []))
                if expected_scope == "department"
                else []
            )
            assert live.get("visibility_scope") == expected_scope, (
                f"verify_subagent_scope_mismatch:{slug}"
            )
            assert live.get("owner_department_id") == expected_dept, (
                f"verify_subagent_owner_dept_mismatch:{slug}"
            )
            assert _normalize_grants(live.get("visibility_grants")) == _normalize_grants(
                expected_grants
            ), f"verify_subagent_grants_mismatch:{slug}"
            expected_tids = self._resolve_allowed_tool_ids(
                cast(Sequence[str], spec.get("allowed_tool_names", ())),
                available_tool_ids=available_tool_ids,
                name_to_tool_id=name_to_tool_id,
                subagent_slug=slug,
            )
            assert list(live.get("allowed_tool_ids") or ()) == expected_tids, (
                f"verify_subagent_tools_mismatch:{slug}"
            )
            assert "dispatch_subagents" not in set(live.get("allowed_tool_ids") or ())
        self.summary.verified_checks.append("subagents_configured_and_tool_whitelists")

        # 7.6 2 个专项 Project 默认技能与知识库绑定验收。
        for spec in cast(list[dict[str, Any]], self._manifest.get("projects", [])):
            code = str(spec["code"])
            owner_client = self._client_for(str(spec["owner_actor"]))
            projects = self._list_all_projects(owner_client)
            matched_proj = next(
                (item for item in projects if item.get("name") == str(spec["name"]).strip()),
                None,
            )
            assert matched_proj is not None, f"verify_project_missing:{code}"
            expected_skill_ids = self._resolve_project_skill_ids(
                owner_client, cast(Sequence[str], spec.get("default_skill_codes", ())), code
            )
            assert list(matched_proj.get("default_skill_ids") or ()) == expected_skill_ids, (
                f"verify_project_skills_mismatch:{code}"
            )
            expected_kb_ids = set(
                self._resolve_project_kb_ids(
                    owner_client, cast(Sequence[str], spec.get("knowledge_base_keys", ())), code
                )
            )
            bound_resp = self._require_status(
                owner_client.get(f"/api/v1/projects/{matched_proj['id']}/knowledge-bases"),
                200,
                f"verify_project_kbs:{code}",
            )
            bound_kb_ids = {
                str(item["id"]) for item in cast(list[dict[str, Any]], bound_resp.json())
            }
            assert expected_kb_ids <= bound_kb_ids, f"verify_project_kb_bindings_missing:{code}"
        self.summary.verified_checks.append("projects_and_knowledge_bindings")

        # 7.7 6 角色偏好与显式记忆基线验收（重点核对 mock_fin_member memory_enabled=False）。
        for role_spec in cast(list[dict[str, Any]], self._manifest["preferences_and_memories"]):
            user_id = str(role_spec["user_id"])
            client = self._client_for(user_id)
            profile = self._require_status(
                client.get("/api/v1/me/preferences"), 200, f"verify_pref:{user_id}"
            ).json()
            assert bool(profile["memory_enabled"]) is bool(role_spec["memory_enabled"]), (
                f"memory_enabled_mismatch:{user_id}"
            )
            mem_data = self._require_status(
                client.get("/api/v1/me/memories"), 200, f"verify_mem:{user_id}"
            ).json()
            existing_norm = {
                _normalize_text(str(item["content"]))
                for item in cast(list[dict[str, Any]], mem_data.get("items", []))
            }
            for expected_mem in cast(list[str], role_spec["manual_memories"]):
                assert _normalize_text(expected_mem) in existing_norm, (
                    f"manual_memory_missing:{user_id}"
                )
        self.summary.verified_checks.append("preferences_and_memories_baseline")

        # 7.8 & 7.9 全部 110 题在线检索量化评测（Hit@1/5/8、MRR）与 13 条 RBAC 零泄漏验收。
        eval_data = json.loads(self._eval_fixture_path.read_text(encoding="utf-8"))
        all_eval_cases = cast(list[dict[str, Any]], eval_data["queries"])
        positive_records: list[dict[str, Any]] = []
        rbac_records: list[dict[str, Any]] = []
        ood_cases = 0
        rbac_leak_count = 0

        for case in all_eval_cases:
            case_id = str(case["id"])
            kind = str(case["kind"])
            actor_id = str(case["actor_user_id"])
            query = str(case["query"])
            actor_client = self._client_for(actor_id)
            if kind == "rbac_isolation":
                readable_bases = self._list_all_bases(actor_client)
                forbidden_live_ids = {
                    doc_ids[code]
                    for code in cast(list[str], case["forbidden_doc_ids"])
                    if code in doc_ids
                }
                leaked_ids: set[str] = set()
                for base in readable_bases:
                    s_resp = self._require_status(
                        actor_client.post(
                            f"/api/v1/knowledge-bases/{base['id']}/search",
                            json={"query": query, "limit": 8},
                        ),
                        200,
                        f"verify_rbac_search:{case_id}",
                    )
                    hit_doc_ids = {
                        str(hit["document_id"])
                        for hit in cast(list[dict[str, Any]], s_resp.json().get("hits", []))
                    }
                    leaked_ids.update(hit_doc_ids & forbidden_live_ids)
                if leaked_ids:
                    rbac_leak_count += len(leaked_ids)
                rbac_records.append(
                    {
                        "id": case_id,
                        "actor_user_id": actor_id,
                        "leaked_doc_ids": sorted(leaked_ids),
                    }
                )
            elif case.get("expected_doc_id") and case.get("expected_kb_key"):
                expected_code = str(case["expected_doc_id"])
                target_kb_id = kb_ids[str(case["expected_kb_key"])]
                expected_live_doc_id = doc_ids[expected_code]
                s_resp = self._require_status(
                    actor_client.post(
                        f"/api/v1/knowledge-bases/{target_kb_id}/search",
                        json={"query": query, "limit": 8},
                    ),
                    200,
                    f"verify_eval_search:{case_id}",
                )
                # 按首次出现顺序对返回命中去重，计算文档级精确排名（1..8 或 None）。
                ordered_doc_ids: list[str] = []
                for hit in cast(list[dict[str, Any]], s_resp.json().get("hits", [])):
                    did = str(hit["document_id"])
                    if did not in ordered_doc_ids:
                        ordered_doc_ids.append(did)
                rank: int | None = (
                    ordered_doc_ids.index(expected_live_doc_id) + 1
                    if expected_live_doc_id in ordered_doc_ids
                    else None
                )
                positive_records.append(
                    {
                        "id": case_id,
                        "kind": kind,
                        "actor_user_id": actor_id,
                        "expected_doc_id": expected_code,
                        "rank": rank,
                        "hit_at_1": rank == 1,
                        "hit_at_5": rank is not None and rank <= 5,
                        "hit_at_8": rank is not None and rank <= 8,
                        "reciprocal_rank": round(1.0 / rank, 4) if rank is not None else 0.0,
                    }
                )
            else:
                ood_cases += 1

        def _summarize_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            count = len(records)
            if count == 0:
                return {
                    "count": 0,
                    "hit_at_1": 0.0,
                    "hit_at_5": 0.0,
                    "hit_at_8": 0.0,
                    "mrr": 0.0,
                }
            return {
                "count": count,
                "hit_at_1": round(sum(1 for r in records if r["hit_at_1"]) / count, 4),
                "hit_at_5": round(sum(1 for r in records if r["hit_at_5"]) / count, 4),
                "hit_at_8": round(sum(1 for r in records if r["hit_at_8"]) / count, 4),
                "mrr": round(
                    sum(float(cast(float, r["reciprocal_rank"])) for r in records) / count, 4
                ),
            }

        global_metrics = _summarize_metrics(positive_records)
        by_kind_buckets: dict[str, list[dict[str, Any]]] = {}
        for rec in positive_records:
            by_kind_buckets.setdefault(str(rec["kind"]), []).append(rec)
        by_kind_metrics = {
            kind_name: _summarize_metrics(bucket)
            for kind_name, bucket in sorted(by_kind_buckets.items())
        }
        hard_neg_records = by_kind_buckets.get("hard_negative_confusable", [])
        hard_neg_top1 = (
            round(sum(1 for r in hard_neg_records if r["rank"] == 1) / len(hard_neg_records), 4)
            if hard_neg_records
            else 0.0
        )
        cross_year_records = by_kind_buckets.get("cross_year_version_disambiguation", [])
        cross_year_top1 = (
            round(sum(1 for r in cross_year_records if r["rank"] == 1) / len(cross_year_records), 4)
            if cross_year_records
            else 0.0
        )

        eval_report: dict[str, Any] = {
            "dataset": str(eval_data.get("dataset", "enterprise_hroa_v3")),
            "total_cases": len(all_eval_cases),
            "positive_cases": len(positive_records),
            "rbac_cases": len(rbac_records),
            "negative_out_of_domain_cases": ood_cases,
            "hit_at_1": global_metrics["hit_at_1"],
            "hit_at_5": global_metrics["hit_at_5"],
            "hit_at_8": global_metrics["hit_at_8"],
            "mrr": global_metrics["mrr"],
            "hard_negative_top1_win_rate": hard_neg_top1,
            "cross_year_top1_win_rate": cross_year_top1,
            "rbac_leak_count": rbac_leak_count,
            "by_kind": by_kind_metrics,
            "cases": positive_records,
            "rbac_results": rbac_records,
        }
        self.summary.retrieval_eval_report = eval_report

        # 若指定 --eval-report，先落盘 JSON，便于命中率低于底线时直接定位漏召回题。
        if self._eval_report_path is not None:
            self._eval_report_path.parent.mkdir(parents=True, exist_ok=True)
            self._eval_report_path.write_text(
                json.dumps(eval_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        assert len(positive_records) >= 90 and len(rbac_records) == 13, (
            f"verify_eval_counts_unexpected positive={len(positive_records)} "
            f"rbac={len(rbac_records)}"
        )
        assert rbac_leak_count == 0, f"rbac_search_leakage_detected leak_count={rbac_leak_count}"
        assert float(eval_report["hit_at_5"]) >= 0.90, (
            f"eval_hit_at_5_below_baseline got={eval_report['hit_at_5']} required=0.90"
        )
        assert float(eval_report["hit_at_8"]) >= 0.95, (
            f"eval_hit_at_8_below_baseline got={eval_report['hit_at_8']} required=0.95"
        )
        assert float(by_kind_metrics["exact_policy_code"]["hit_at_5"]) == 1.0, (
            f"exact_policy_code_hit_at_5_must_be_1 got={by_kind_metrics['exact_policy_code']}"
        )
        assert float(by_kind_metrics["identifier_entity"]["hit_at_5"]) == 1.0, (
            f"identifier_entity_hit_at_5_must_be_1 got={by_kind_metrics['identifier_entity']}"
        )
        self.summary.verified_checks.append("enterprise_retrieval_eval_positive_and_confusable")
        self.summary.verified_checks.append("enterprise_retrieval_eval_and_rbac_zero_leak")

        # 7.10 可选组织停用与原 Cookie 即时 401 演练（默认关闭以保持 --verify 纯只读）。
        if self._verify_org_lifecycle:
            # 停用前确认原 Cookie 有效，确保随后的 401 确实由停用触发。
            self._require_status(
                fin_member_client.get("/api/v1/auth/me"),
                200,
                "verify_org_lifecycle_precheck",
            )
            try:
                self._require_status(
                    super_client.put(
                        "/api/v1/admin/org/users/mock_fin_member/status",
                        json={"status": "disabled"},
                    ),
                    200,
                    "verify_org_lifecycle_disable",
                )
                revoked_resp = fin_member_client.get("/api/v1/auth/me")
                assert revoked_resp.status_code == 401, (
                    f"disabled_user_cookie_must_return_401 got={revoked_resp.status_code}"
                )
            finally:
                # 无论演练成败，均在 finally 中立即恢复 mock_fin_member 为 active 并清理失效客户端。
                self._require_status(
                    super_client.put(
                        "/api/v1/admin/org/users/mock_fin_member/status",
                        json={"status": "active"},
                    ),
                    200,
                    "verify_org_lifecycle_restore",
                )
                stale_client = self._clients.pop("mock_fin_member", None)
                if stale_client is not None:
                    stale_client.close()
            self.summary.verified_checks.append(
                "org_lifecycle_disable_revokes_session_and_restores"
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        description=(
            "Athena v2 企业场景测试数据体系（V1 + V2 + V3，6 库 36 档 / 110 题量化评测）"
            "一键幂等导入与多角色验收工具"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="Athena 后端服务基址（默认 http://localhost:8080）",
    )
    parser.add_argument(
        "--mcp-runtime",
        choices=("docker", "host"),
        default="docker",
        help="stdio MCP 启动路径模式：docker（默认）或 host",
    )
    parser.add_argument(
        "--hroa-state-file",
        default=None,
        help="覆盖 HR/OA MCP 共享状态 JSON 路径",
    )
    parser.add_argument(
        "--docker-container",
        default=None,
        help="Docker 模式下配合 --reset-hroa-state 指定执行容器名（如 athena-backend-api）",
    )
    parser.add_argument(
        "--only",
        default=",".join(ALL_MODULES),
        help="逗号分隔执行模块子集：knowledge,memory,mcp,skills,subagents,projects（默认全量）",
    )
    parser.add_argument(
        "--rebuild-assets",
        action="store_true",
        help="导入前重新运行 build_assets.py 生成确定性多格式样本",
    )
    parser.add_argument(
        "--reset-hroa-state",
        action="store_true",
        help="重置本地 HR/OA MCP 状态文件至 2026-11-24 基线",
    )
    parser.add_argument(
        "--reset-memories",
        action="store_true",
        help="先清理 6 角色已有 manual 显式记忆再重建（保留 chat_explicit）",
    )
    parser.add_argument(
        "--skip-kb-ocr",
        action="store_true",
        help="跳过需 PaddleOCR 的知识库图片样本（DOC-06 / DOC-29）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="执行端到端在线多角色 RBAC、MCP、Skills、偏好记忆与 110 题量化检索验收",
    )
    parser.add_argument(
        "--eval-report",
        default=None,
        help="配合 --verify 使用：将 110 题检索量化评测 JSON 报告写入指定路径",
    )
    parser.add_argument(
        "--verify-org-lifecycle",
        action="store_true",
        help=(
            "配合 --verify 使用：在 try/finally 内短暂停用 mock_fin_member 验证原 Cookie 立即 401，"
            "并在结束后恢复 active（默认关闭以保持纯只读）"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印计划动作，不执行任何写请求",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=120.0,
        help="知识库文档入库就绪轮询超时时间（秒，默认 120）",
    )
    return parser.parse_args(argv)


def _parse_selected_modules(raw_only: str) -> tuple[str, ...]:
    """校验并规范化 --only 模块列表。"""
    parts = [segment.strip().lower() for segment in raw_only.split(",") if segment.strip()]
    if not parts:
        return ALL_MODULES
    invalid = [part for part in parts if part not in ALL_MODULES]
    if invalid:
        raise ValueError(f"unsupported_modules:{','.join(invalid)}")
    return tuple(dict.fromkeys(parts))


def main(argv: Sequence[str] | None = None) -> None:
    """CLI 主入口。"""
    args = parse_args(argv)
    modules = _parse_selected_modules(args.only)

    if args.rebuild_assets:
        if args.dry_run:
            print("[dry-run] rebuild assets via build_assets.py")
        else:
            build_all_assets(GENERATED_DIR)

    if args.reset_hroa_state:
        try:
            reset_hroa_state_safely(
                mcp_runtime=args.mcp_runtime,
                state_file=args.hroa_state_file,
                docker_container=args.docker_container,
                dry_run=args.dry_run,
            )
        except RuntimeError as error:
            raise SystemExit(str(error)) from error

    eval_report_path = Path(args.eval_report).expanduser() if args.eval_report else None
    seeder = EnterpriseSeeder(
        base_url=args.base_url,
        mcp_runtime=args.mcp_runtime,
        hroa_state_file=args.hroa_state_file,
        skip_kb_ocr=args.skip_kb_ocr,
        reset_memories=args.reset_memories,
        dry_run=args.dry_run,
        wait_timeout=args.wait_timeout,
        eval_report_path=eval_report_path,
        verify_org_lifecycle=bool(args.verify_org_lifecycle),
    )
    try:
        if "knowledge" in modules:
            seeder.seed_knowledge()
        if "memory" in modules:
            seeder.seed_memories()
        if "mcp" in modules:
            seeder.seed_mcp()
        if "skills" in modules:
            seeder.seed_skills()
        if "subagents" in modules:
            seeder.seed_subagents()
        if "projects" in modules:
            seeder.seed_projects()
        if args.verify:
            seeder.verify_all()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "dry_run": args.dry_run,
                    "modules": list(modules),
                    "created": seeder.summary.created,
                    "updated": seeder.summary.updated,
                    "skipped": seeder.summary.skipped,
                    "warnings": seeder.summary.warnings,
                    "verified_checks": seeder.summary.verified_checks,
                    "retrieval_eval_report": seeder.summary.retrieval_eval_report,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        seeder.close()


if __name__ == "__main__":
    main()
