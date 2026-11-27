"""企业知识检索评测集（110 题 / 9 类）离线覆盖校验与一键导入脚本幂等/量化验收测试。"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml  # type: ignore[import-untyped]

from infrastructure.knowledge.parsers import LocalDocumentParser
from mcp_servers.hroa.state import HroaStateStore

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_enterprise_data import (  # noqa: E402
    EnterpriseSeeder,
    reset_hroa_state_safely,
)

EVAL_PATH = (
    REPO_ROOT / "backend" / "tests" / "fixtures" / "enterprise_knowledge_retrieval_eval.json"
)
DATASET_ROOT = REPO_ROOT / "datasets" / "enterprise_hroa"
GENERATED_DIR = DATASET_ROOT / "generated"
MANIFEST_PATH = DATASET_ROOT / "manifest.yaml"


def _actor_can_read_kb(actor_id: str, kb_spec: dict[str, Any]) -> bool:
    """按 Athena 三层权限语义离线判定指定 Mock 角色是否可读目标知识库。"""
    users: dict[str, dict[str, Any]] = {
        "mock_super_admin": {
            "role": "super_admin",
            "ancestors": {"dept_mock_root"},
            "managed": {
                "dept_mock_root",
                "dept_mock_rd",
                "dept_mock_platform",
                "dept_mock_fin",
                "dept_mock_hr",
            },
        },
        "mock_fin_admin": {
            "role": "dept_admin",
            "ancestors": {"dept_mock_root", "dept_mock_fin"},
            "managed": {"dept_mock_fin"},
        },
        "mock_rd_admin": {
            "role": "dept_admin",
            "ancestors": {"dept_mock_root", "dept_mock_rd"},
            "managed": {"dept_mock_rd", "dept_mock_platform"},
        },
        "mock_fin_member": {
            "role": "member",
            "ancestors": {"dept_mock_root", "dept_mock_fin"},
            "managed": set(),
        },
        "mock_rd_member": {
            "role": "member",
            "ancestors": {"dept_mock_root", "dept_mock_rd", "dept_mock_platform"},
            "managed": set(),
        },
        "mock_hr_member": {
            "role": "member",
            "ancestors": {"dept_mock_root", "dept_mock_hr"},
            "managed": set(),
        },
    }
    user = users[actor_id]
    scope = str(kb_spec["visibility_scope"])
    if scope == "personal":
        return str(kb_spec["owner_actor"]) == actor_id
    if scope == "company":
        return True
    if user["role"] == "super_admin":
        return True
    if user["role"] == "dept_admin" and kb_spec.get("owner_department_id") in user["managed"]:
        return True
    for grant in cast(list[dict[str, Any]], kb_spec.get("visibility_grants", [])):
        if grant.get("target_type") == "department" and grant.get("target_id") in user["ancestors"]:
            return True
    return False


def test_enterprise_retrieval_eval_110_cases_structure_permissions_and_keyword_coverage() -> None:
    """校验 110 题 9 类结构、保留 V1+V2 全部 76 题、同权混淆/严格隔离边界与关键词 100% 存在。"""
    payload = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    manifest = cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))
    kb_by_key = {
        str(kb["key"]): kb for kb in cast(list[dict[str, Any]], manifest["knowledge_bases"])
    }
    queries = cast(list[dict[str, Any]], payload["queries"])
    docs_index = {
        str(item["doc_id"]): item for item in cast(list[dict[str, Any]], payload["documents"])
    }

    assert len(docs_index) == 36
    assert len(queries) == 110
    query_ids = [str(q["id"]) for q in queries]
    assert len(set(query_ids)) == 110
    # 强验证向后兼容：V1 + V2 全部 76 题 ID 原样保留。
    v1_v2_ids = {f"EVAL-{idx:02d}" for idx in range(1, 77)}
    assert v1_v2_ids <= set(query_ids)

    kind_counts = Counter(str(q["kind"]) for q in queries)
    assert dict(kind_counts) == {
        "exact_policy_code": 15,
        "identifier_entity": 14,
        "table_cell_numeric": 16,
        "cross_format_locator": 15,
        "colloquial_paraphrase": 15,
        "negative_out_of_domain": 4,
        "hard_negative_confusable": 10,
        "rbac_isolation": 13,
        "cross_year_version_disambiguation": 8,
    }
    assert dict(kind_counts) == payload["category_counts"]

    valid_actors = {
        "mock_super_admin",
        "mock_fin_admin",
        "mock_rd_admin",
        "mock_fin_member",
        "mock_rd_member",
        "mock_hr_member",
    }
    parser = LocalDocumentParser(enable_ocr=False)
    parsed_cache: dict[str, tuple[str, set[str]]] = {}

    def get_parsed(doc_id: str) -> tuple[str, set[str]]:
        if doc_id not in parsed_cache:
            doc_meta = docs_index[doc_id]
            file_path = GENERATED_DIR / str(doc_meta["file"])
            parsed = parser.parse_sync(file_path.name, file_path.read_bytes())
            full_text = "\n".join(block.text for block in parsed.blocks)
            locator_kinds = {block.locator.kind for block in parsed.blocks}
            parsed_cache[doc_id] = (full_text, locator_kinds)
        return parsed_cache[doc_id]

    covered_non_ocr_docs: set[str] = set()
    for case in queries:
        actor_id = str(case["actor_user_id"])
        assert actor_id in valid_actors
        kind = str(case["kind"])
        expected_doc = case["expected_doc_id"]
        forbidden_ids = cast(list[str], case["forbidden_doc_ids"])

        if kind == "rbac_isolation":
            assert expected_doc is None
            assert forbidden_ids, f"rbac_needs_forbidden:{case['id']}"
            # 严格校验：RBAC 隔离题的每个禁读文档对当前角色确实无权访问。
            for f_doc in forbidden_ids:
                f_kb = kb_by_key[str(docs_index[f_doc]["kb_key"])]
                assert not _actor_can_read_kb(actor_id, f_kb), (
                    f"rbac_forbidden_doc_actually_readable case={case['id']} "
                    f"actor={actor_id} doc={f_doc}"
                )
            continue

        if kind == "negative_out_of_domain":
            assert expected_doc is None
            assert forbidden_ids, f"negative_needs_forbidden:{case['id']}"
            continue

        assert isinstance(expected_doc, str) and expected_doc in docs_index
        assert case["expected_kb_key"] == docs_index[expected_doc]["kb_key"]
        assert _actor_can_read_kb(actor_id, kb_by_key[str(case["expected_kb_key"])]), (
            f"positive_expected_doc_not_readable case={case['id']} actor={actor_id}"
        )

        if kind in {"hard_negative_confusable", "cross_year_version_disambiguation"}:
            assert forbidden_ids, f"confusable_or_cross_year_needs_distractor:{case['id']}"
            # 严格校验：干扰项与目标文档对当前角色同权可读，确保测的是语义区分而非权限过滤。
            for d_doc in forbidden_ids:
                d_kb = kb_by_key[str(docs_index[d_doc]["kb_key"])]
                assert _actor_can_read_kb(actor_id, d_kb), (
                    f"distractor_not_same_permission case={case['id']} "
                    f"actor={actor_id} distractor={d_doc}"
                )

        covered_non_ocr_docs.add(expected_doc)
        full_text, locator_kinds = get_parsed(expected_doc)
        expected_keywords = cast(list[str], case["expected_keywords"])
        assert expected_keywords, f"positive_case_missing_keywords:{case['id']}"
        for keyword in expected_keywords:
            assert keyword in full_text, (
                f"eval_keyword_missing case={case['id']} doc={expected_doc} keyword={keyword!r}"
            )
        expected_loc = case.get("expected_locator_kind")
        if isinstance(expected_loc, str):
            assert expected_loc in locator_kinds, (
                f"eval_locator_mismatch case={case['id']} expected={expected_loc} "
                f"got={locator_kinds}"
            )

    # 确认除 DOC-06 与 DOC-29（两份 OCR 流程图）外全部 34 份非 OCR 文档均被评测题覆盖。
    expected_non_ocr_docs = {f"DOC-{idx:02d}" for idx in range(1, 37) if idx not in {6, 29}}
    assert expected_non_ocr_docs <= covered_non_ocr_docs


class _SimulatedAthenaBackend:
    """模拟 Athena v2 核心 REST 接口、幂等槽与三层 RBAC 语义的内存后端。"""

    def __init__(self, *, fail_ocr_uploads: bool = False) -> None:
        self.requests: list[tuple[str, str]] = []
        self.fail_ocr_uploads = fail_ocr_uploads
        self._seq = 0
        self._parser = LocalDocumentParser(enable_ocr=False)

        # 6 个 Mock 用户的角色、祖先部门链与管辖部门映射（与 mock_seed.py 一致）。
        self._users: dict[str, dict[str, Any]] = {
            "mock_super_admin": {
                "role": "super_admin",
                "ancestors": {"dept_mock_root"},
                "managed": {
                    "dept_mock_root",
                    "dept_mock_rd",
                    "dept_mock_platform",
                    "dept_mock_fin",
                    "dept_mock_hr",
                },
            },
            "mock_fin_admin": {
                "role": "dept_admin",
                "ancestors": {"dept_mock_root", "dept_mock_fin"},
                "managed": {"dept_mock_fin"},
            },
            "mock_rd_admin": {
                "role": "dept_admin",
                "ancestors": {"dept_mock_root", "dept_mock_rd"},
                "managed": {"dept_mock_rd", "dept_mock_platform"},
            },
            "mock_fin_member": {
                "role": "member",
                "ancestors": {"dept_mock_root", "dept_mock_fin"},
                "managed": set(),
            },
            "mock_rd_member": {
                "role": "member",
                "ancestors": {"dept_mock_root", "dept_mock_rd", "dept_mock_platform"},
                "managed": set(),
            },
            "mock_hr_member": {
                "role": "member",
                "ancestors": {"dept_mock_root", "dept_mock_hr"},
                "managed": set(),
            },
        }

        self.kbs: dict[str, dict[str, Any]] = {}
        self.docs: dict[str, dict[str, Any]] = {}
        self.doc_sources: dict[str, bytes] = {}
        self.doc_texts: dict[str, str] = {}
        self.preferences: dict[str, dict[str, Any]] = {}
        # 预先给 mock_hr_member 放一条 chat_explicit 记忆，
        # 验证幂等重跑与 --reset-memories 均不误删。
        self.memories: dict[str, list[dict[str, Any]]] = {
            "mock_hr_member": [
                {
                    "id": "umem_chat_keep_1",
                    "content": "对话显式保存：用户希望周报结尾附带下周三件最紧急事项。",
                    "source": "chat_explicit",
                    "created_at": "2026-11-24T08:00:00Z",
                    "updated_at": "2026-11-24T08:00:00Z",
                }
            ]
        }
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self.mcp_tools: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.subagents: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.project_kb_bindings: dict[str, list[str]] = {}
        # 模拟 Athena 真实后端的文档替换与 Skill 发布幂等槽，用于回归重复恢复基线场景。
        self.doc_replace_idem: dict[tuple[str, str], tuple[str, str]] = {}
        self.skill_idem: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
        # 模拟真实用户启停状态与独立会话令牌映射，支持 --verify-org-lifecycle 停用即吊销 401 演练。
        self.user_status: dict[str, str] = {uid: "active" for uid in self._users}
        self.active_sessions: dict[str, str] = {}

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def _actor(self, request: httpx.Request) -> str:
        cookie_header = str(request.headers.get("cookie", ""))
        for part in cookie_header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "athena_session":
                token = str(v)
                actor = self.active_sessions.get(token)
                if actor is not None and self.user_status.get(actor) == "active":
                    return actor
                return ""
        return ""

    def _can_read_resource(self, actor: str, resource: dict[str, Any]) -> bool:
        user = self._users[actor]
        scope = resource.get("visibility_scope") or resource.get("scope")
        if scope == "personal":
            return resource.get("owner_id") == actor
        if scope == "company":
            return True
        if user["role"] == "super_admin":
            return True
        if user["role"] == "dept_admin" and resource.get("owner_department_id") in user["managed"]:
            return True
        for grant in cast(list[dict[str, Any]], resource.get("visibility_grants", [])):
            if (
                grant.get("target_type") == "department"
                and grant.get("target_id") in user["ancestors"]
            ):
                return True
        return False

    def _validate_grants(self, actor: str, grants: list[dict[str, Any]]) -> bool:
        user = self._users[actor]
        if user["role"] == "super_admin":
            return True
        return all(g.get("target_id") in user["managed"] for g in grants)

    def _parse_multipart_file(self, request: httpx.Request) -> tuple[str, bytes, dict[str, str]]:
        """轻量解析 httpx multipart/form-data 负载，提取表单字段与首个上传文件。"""
        content_type = request.headers.get("content-type", "")
        boundary = content_type.split("boundary=")[-1].strip().strip('"').encode("ascii")
        delimiter = b"--" + boundary
        fields: dict[str, str] = {}
        filename = "upload.bin"
        file_bytes = b""
        for part in request.content.split(delimiter)[1:]:
            if part.startswith(b"--"):
                break
            part = part.lstrip(b"\r\n")
            head, _, tail = part.partition(b"\r\n\r\n")
            body = tail[:-2] if tail.endswith(b"\r\n") else tail
            headers_text = head.decode("utf-8", errors="replace")
            name = ""
            part_filename = None
            for line in headers_text.splitlines():
                if line.lower().startswith("content-disposition:"):
                    for token in line.split(";"):
                        token = token.strip()
                        if token.startswith("name="):
                            name = token.split("=", 1)[1].strip('"')
                        elif token.startswith("filename="):
                            part_filename = token.split("=", 1)[1].strip('"')
            if part_filename is not None:
                filename = part_filename
                file_bytes = body
            elif name:
                fields[name] = body.decode("utf-8")
        return filename, file_bytes, fields

    def handle(self, request: httpx.Request) -> httpx.Response:
        """分发并处理全部模拟 REST API 请求。"""
        method = request.method
        path = request.url.path
        self.requests.append((method, path))

        if method != "GET" and not request.headers.get("origin"):
            return httpx.Response(403, json={"detail": "origin_missing"})

        # 1. Mock 登录（仅允许 active 用户签发真实会话令牌，停用后立即失效）。
        if method == "POST" and path == "/api/v1/auth/mock/login":
            user_id = str(json.loads(request.content)["user_id"])
            if user_id not in self._users:
                return httpx.Response(404, json={"detail": "mock_user_not_found"})
            if self.user_status.get(user_id) != "active":
                return httpx.Response(403, json={"detail": "user_disabled"})
            session_token = self._next_id(f"sess_{user_id}")
            self.active_sessions[session_token] = user_id
            return httpx.Response(
                200,
                json={"id": user_id},
                headers={"Set-Cookie": f"athena_session={session_token}; Path=/; HttpOnly"},
            )

        actor = self._actor(request)
        if actor not in self._users:
            return httpx.Response(401, json={"detail": "not_authenticated"})

        if method == "GET" and path == "/api/v1/auth/me":
            return httpx.Response(
                200,
                json={
                    "id": actor,
                    "role": self._users[actor]["role"],
                    "status": self.user_status[actor],
                },
            )

        # 1b. 超管组织用户启停接口（停用同事务吊销该用户全部活跃会话）。
        if path.startswith("/api/v1/admin/org/users/") and path.endswith("/status"):
            if method == "PUT":
                if self._users[actor]["role"] != "super_admin":
                    return httpx.Response(403, json={"detail": "admin_required"})
                target_uid = path.removeprefix("/api/v1/admin/org/users/").removesuffix("/status")
                if target_uid not in self._users:
                    return httpx.Response(404, json={"detail": "org_user_not_found"})
                new_status = str(json.loads(request.content)["status"])
                self.user_status[target_uid] = new_status
                if new_status == "disabled":
                    for tok in [t for t, u in self.active_sessions.items() if u == target_uid]:
                        self.active_sessions.pop(tok, None)
                return httpx.Response(200, json={"id": target_uid, "status": new_status})

        # 2. 知识库与文档接口。
        if path == "/api/v1/knowledge-bases":
            if method == "GET":
                visible = [kb for kb in self.kbs.values() if self._can_read_resource(actor, kb)]
                return httpx.Response(200, json={"items": visible, "page": 1, "page_size": 100})
            if method == "POST":
                body = json.loads(request.content)
                kb_id = self._next_id("kb")
                record = {
                    "id": kb_id,
                    "name": body["name"],
                    "description": body.get("description"),
                    "visibility_scope": body.get("visibility_scope", "personal"),
                    "owner_id": actor,
                    "owner_department_id": body.get("owner_department_id"),
                    "visibility_grants": body.get("visibility_grants") or [],
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                }
                self.kbs[kb_id] = record
                return httpx.Response(201, json=record)

        if path.startswith("/api/v1/knowledge-bases/"):
            rest = path.removeprefix("/api/v1/knowledge-bases/")
            parts = rest.split("/")
            base_id = parts[0]
            kb = self.kbs.get(base_id)
            if kb is None or not self._can_read_resource(actor, kb):
                return httpx.Response(404, json={"detail": "knowledge_base_not_found"})

            if len(parts) == 1:
                if method == "GET":
                    return httpx.Response(200, json=kb)
                if method == "PATCH":
                    body = json.loads(request.content)
                    if "visibility_grants" in body and not self._validate_grants(
                        actor, body["visibility_grants"]
                    ):
                        return httpx.Response(403, json={"detail": "visibility_grant_forbidden"})
                    for field in ("name", "description", "visibility_grants"):
                        if field in body and body[field] is not None:
                            kb[field] = body[field]
                    return httpx.Response(200, json=kb)

            if len(parts) == 2 and parts[1] == "search" and method == "POST":
                query_text = str(json.loads(request.content)["query"])
                tokens = _query_tokens(query_text)
                hits: list[dict[str, Any]] = []
                for doc in self.docs.values():
                    if doc["knowledge_base_id"] != base_id:
                        continue
                    content_text = self.doc_texts.get(doc["id"], "")
                    title_text = str(doc["title"])
                    # 简化的离线加权匹配：标题命中加权，制度编号/工号/单号精确命中额外加权。
                    score = 0
                    for token in tokens:
                        in_title = token in title_text
                        in_body = token in content_text
                        if not (in_title or in_body):
                            continue
                        is_identifier = (
                            "-" in token
                            or "@" in token
                            or token.startswith(
                                ("XC", "MOCK", "TRV", "CTR", "FIN", "OFFER", "ONC", "LEG", "PERF")
                            )
                        )
                        weight = 5 if is_identifier else 1
                        score += weight * (3 if in_title else 1)
                    if score > 0:
                        hits.append(
                            {
                                "chunk_id": f"chk_{doc['id']}",
                                "chunk_ids": [f"chk_{doc['id']}"],
                                "document_id": doc["id"],
                                "version_id": doc["active_version_id"],
                                "title": title_text,
                                "text": content_text[:200],
                                "locators": [],
                                "channels": ["keyword"],
                                "_score": score,
                            }
                        )
                hits.sort(key=lambda item: int(item["_score"]), reverse=True)
                for h in hits:
                    h.pop("_score", None)
                return httpx.Response(
                    200, json={"hits": hits[:8], "channels": ["keyword"], "degraded_reasons": []}
                )

            if len(parts) == 2 and parts[1] == "documents" and method == "GET":
                base_docs = [d for d in self.docs.values() if d["knowledge_base_id"] == base_id]
                return httpx.Response(200, json={"items": base_docs, "page": 1, "page_size": 100})

            if len(parts) == 3 and parts[1] == "documents" and parts[2] == "upload":
                filename, content, fields = self._parse_multipart_file(request)
                doc_id = self._next_id("doc")
                ver_id = self._next_id("docver")
                is_img = Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg"}
                ocr_failed = is_img and self.fail_ocr_uploads
                extracted = ""
                if not is_img:
                    parsed = self._parser.parse_sync(filename, content)
                    extracted = "\n".join(block.text for block in parsed.blocks)
                self.doc_sources[ver_id] = content
                self.doc_texts[doc_id] = extracted
                self.docs[doc_id] = {
                    "id": doc_id,
                    "knowledge_base_id": base_id,
                    "title": fields["title"],
                    "active_version_id": None if ocr_failed else ver_id,
                    "pending_version_id": None,
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                    "latest_version": {
                        "id": ver_id,
                        "document_id": doc_id,
                        "state": "failed" if ocr_failed else "ready",
                        "error_code": "knowledge_ocr_engine_unavailable" if ocr_failed else None,
                        "warnings": [],
                        "chunk_count": 0 if ocr_failed else 1,
                        "created_at": "2026-11-24T00:00:00Z",
                        "completed_at": "2026-11-24T00:00:01Z",
                    },
                }
                return httpx.Response(
                    202,
                    json={
                        "document_id": doc_id,
                        "version_id": ver_id,
                        "state": "queued",
                        "status_url": f"/api/v1/knowledge-bases/{base_id}/documents/{doc_id}",
                    },
                )

            if (
                len(parts) == 5
                and parts[1] == "documents"
                and parts[3] == "versions"
                and parts[4] == "upload"
                and method == "POST"
            ):
                doc_id = parts[2]
                target_doc = self.docs.get(doc_id)
                if target_doc is None:
                    return httpx.Response(404, json={"detail": "document_not_found"})
                idem_key = str(request.headers.get("idempotency-key", "")).strip()
                if not idem_key or len(idem_key) > 128:
                    return httpx.Response(422, json={"detail": "idempotency_key_invalid"})
                filename, content, _ = self._parse_multipart_file(request)
                content_sha = hashlib.sha256(content).hexdigest()
                slot_key = (doc_id, idem_key)
                if slot_key in self.doc_replace_idem:
                    prev_ver_id, prev_sha = self.doc_replace_idem[slot_key]
                    if prev_sha != content_sha:
                        return httpx.Response(409, json={"detail": "idempotency_conflict"})
                    # 模拟真实后端幂等重放：直接返回旧版本而不切换 active_version_id。
                    return httpx.Response(
                        202,
                        json={
                            "document_id": doc_id,
                            "version_id": prev_ver_id,
                            "state": "queued",
                            "status_url": f"/api/v1/knowledge-bases/{base_id}/documents/{doc_id}",
                        },
                    )
                ver_id = self._next_id("docver")
                is_img = Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg"}
                extracted = ""
                if not is_img:
                    parsed = self._parser.parse_sync(filename, content)
                    extracted = "\n".join(block.text for block in parsed.blocks)
                self.doc_sources[ver_id] = content
                self.doc_texts[doc_id] = extracted
                target_doc["active_version_id"] = ver_id
                target_doc["latest_version"] = {
                    "id": ver_id,
                    "document_id": doc_id,
                    "state": "ready",
                    "error_code": None,
                    "warnings": [],
                    "chunk_count": 1,
                    "created_at": "2026-11-24T00:00:00Z",
                    "completed_at": "2026-11-24T00:00:01Z",
                }
                self.doc_replace_idem[slot_key] = (ver_id, content_sha)
                return httpx.Response(
                    202,
                    json={
                        "document_id": doc_id,
                        "version_id": ver_id,
                        "state": "queued",
                        "status_url": f"/api/v1/knowledge-bases/{base_id}/documents/{doc_id}",
                    },
                )

            if len(parts) == 3 and parts[1] == "documents" and method == "GET":
                found_doc = self.docs.get(parts[2])
                if found_doc is None:
                    return httpx.Response(404, json={"detail": "document_not_found"})
                return httpx.Response(200, json=found_doc)

            if (
                len(parts) == 6
                and parts[1] == "documents"
                and parts[3] == "versions"
                and parts[5] == "source"
                and method == "GET"
            ):
                ver_id = parts[4]
                if ver_id not in self.doc_sources:
                    return httpx.Response(404, json={"detail": "version_not_found"})
                return httpx.Response(200, content=self.doc_sources[ver_id])

        # 3. 偏好与记忆接口。
        if path == "/api/v1/me/preferences":
            if method == "GET":
                prof = self.preferences.get(
                    actor,
                    {"custom_instructions": "", "memory_enabled": True, "updated_at": None},
                )
                return httpx.Response(200, json=prof)
            if method == "PUT":
                body = json.loads(request.content)
                prof = {
                    "custom_instructions": body["custom_instructions"],
                    "memory_enabled": body["memory_enabled"],
                    "updated_at": "2026-11-24T00:00:00Z",
                }
                self.preferences[actor] = prof
                return httpx.Response(200, json=prof)

        if path == "/api/v1/me/memories":
            bucket = self.memories.setdefault(actor, [])
            if method == "GET":
                return httpx.Response(
                    200,
                    json={"items": list(bucket), "total": len(bucket), "limit": 50},
                )
            if method == "POST":
                body = json.loads(request.content)
                item = {
                    "id": self._next_id("umem"),
                    "content": body["content"],
                    "source": "manual",
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                }
                bucket.append(item)
                return httpx.Response(201, json=item)

        if path.startswith("/api/v1/me/memories/") and method == "DELETE":
            mem_id = path.removeprefix("/api/v1/me/memories/")
            bucket = self.memories.setdefault(actor, [])
            self.memories[actor] = [item for item in bucket if item["id"] != mem_id]
            return httpx.Response(204)

        # 4. MCP Server 与工具接口。
        if path == "/api/v1/admin/mcp/servers":
            if method == "GET":
                items = list(self.mcp_servers.values())
                return httpx.Response(
                    200, json={"items": items, "total": len(items), "page": 1, "page_size": 100}
                )
            if method == "POST":
                body = json.loads(request.content)
                server_id = self._next_id("mcp")
                args_list = list(body.get("args", ()))
                if "fin_ops" in args_list:
                    profile = "fin_ops"
                elif "hr_ops" in args_list:
                    profile = "hr_ops"
                else:
                    profile = "standard"
                server = {
                    "id": server_id,
                    "name": body["name"],
                    "transport": "stdio",
                    "config": {
                        "transport": "stdio",
                        "command": body["command"],
                        "args": body["args"],
                        "cwd": body.get("cwd"),
                    },
                    "secrets": [],
                    "config_version": 1,
                    "enabled": True,
                    "visibility_scope": body.get("visibility_scope", "company"),
                    "owner_department_id": body.get("owner_department_id"),
                    "visibility_grants": body.get("visibility_grants") or [],
                    "created_by": actor,
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                    "tools_discovered_at": "2026-11-24T00:00:00Z",
                }
                self.mcp_servers[server_id] = server
                remote_names_by_profile = {
                    "standard": [
                        "hroa_get_employee_profile",
                        "hroa_get_leave_balance",
                        "hroa_list_oa_tickets",
                        "hroa_check_offboarding_clearance",
                        "hroa_submit_leave_request",
                        "hroa_submit_transfer_request",
                        "hroa_get_performance_review",
                        "hroa_submit_contract_seal_request",
                        "hroa_submit_it_asset_request",
                    ],
                    "hr_ops": [
                        "hroa_list_department_roster",
                        "hroa_initiate_onboarding",
                        "hroa_initiate_offboarding",
                        "hroa_update_clearance_item",
                        "hroa_submit_performance_calibration",
                    ],
                    "fin_ops": [
                        "hroa_list_travel_expenses",
                        "hroa_flag_expense_audit_result",
                    ],
                }
                for remote_name in remote_names_by_profile[profile]:
                    tid = self._next_id("tool")
                    self.mcp_tools[tid] = {
                        "tool_id": tid,
                        "model_name": f"mcp__{tid}",
                        "description": remote_name,
                        "input_schema": {"type": "object"},
                        "server_id": server_id,
                        "remote_name": remote_name,
                        "requires_approval": False,
                        "display_name": None,
                        "approval_summary_template": None,
                        "parameter_labels": {},
                        "result_labels": {},
                    }
                return httpx.Response(201, json=server)

        if path.startswith("/api/v1/admin/mcp/servers/"):
            mcp_parts = path.removeprefix("/api/v1/admin/mcp/servers/").split("/")
            server_id = mcp_parts[0]
            if len(mcp_parts) == 1 and method == "PATCH":
                body = json.loads(request.content)
                server = self.mcp_servers[server_id]
                if int(body["expected_config_version"]) != int(server["config_version"]):
                    return httpx.Response(409, json={"detail": "mcp_config_version_conflict"})
                if "config" in body and body["config"] is not None:
                    server["config"].update(body["config"])
                for field in (
                    "visibility_scope",
                    "owner_department_id",
                    "visibility_grants",
                    "enabled",
                ):
                    if field in body and body[field] is not None:
                        server[field] = body[field]
                server["config_version"] = int(server["config_version"]) + 1
                return httpx.Response(200, json=server)
            if len(mcp_parts) == 2 and mcp_parts[1] == "tools" and method == "GET":
                tools = [t for t in self.mcp_tools.values() if t["server_id"] == server_id]
                return httpx.Response(
                    200, json={"items": tools, "total": len(tools), "page": 1, "page_size": 100}
                )
            if len(mcp_parts) == 2 and mcp_parts[1] == "rediscover" and method == "POST":
                return httpx.Response(200, json=self.mcp_servers[server_id])

        if path.startswith("/api/v1/admin/tools/") and method == "PATCH":
            tool_id = path.removeprefix("/api/v1/admin/tools/")
            body = json.loads(request.content)
            tool = self.mcp_tools[tool_id]
            for key in (
                "requires_approval",
                "display_name",
                "approval_summary_template",
                "parameter_labels",
                "result_labels",
            ):
                if key in body:
                    tool[key] = body[key]
            return httpx.Response(200, json=tool)

        if path == "/api/v1/tools" and method == "GET":
            page = int(request.url.params.get("page", "1"))
            page_size = int(request.url.params.get("page_size", "20"))
            visible_tools = [
                {
                    "tool_id": t["tool_id"],
                    "display_name": t["display_name"],
                    "description": t["description"],
                }
                for t in self.mcp_tools.values()
                if self._can_read_resource(actor, self.mcp_servers[t["server_id"]])
            ]
            start = (page - 1) * page_size
            sliced = visible_tools[start : start + page_size]
            return httpx.Response(
                200,
                json={
                    "items": sliced,
                    "total": len(visible_tools),
                    "page": page,
                    "page_size": page_size,
                },
            )

        # 5. Skills 接口（管理端在线 Markdown / ZIP 导入、个人 Skill 及用户目录）。
        if path == "/api/v1/admin/skills" and method == "GET":
            items = [s for s in self.skills.values() if s["scope"] != "personal"]
            return httpx.Response(200, json={"items": items})

        if path == "/api/v1/admin/skills/markdown" and method == "POST":
            body = json.loads(request.content)
            sid = self._next_id("skill")
            vid = self._next_id("skillver")
            record = {
                "id": sid,
                "slug": body["slug"],
                "scope": body["scope"],
                "owner_id": actor,
                "owner_department_id": body.get("owner_department_id"),
                "visibility_grants": body.get("visibility_grants") or [],
                "name": body["name"],
                "description": body["description"],
                "current_version_id": vid,
                "enabled": body.get("enabled", True),
                "script_requires_approval": False,
                "revision": 1,
                "format": "markdown",
                "featured": body.get("featured", False),
                "starter_prompt": body.get("starter_prompt"),
                "versions": [
                    {
                        "id": vid,
                        "name": body["name"],
                        "description": body["description"],
                        "guide": body["guide"],
                        "package_sha256": "",
                    }
                ],
            }
            self.skills[sid] = record
            return httpx.Response(201, json=record)

        if path == "/api/v1/admin/skills/import" and method == "POST":
            pkg_filename, pkg_bytes, fields = self._parse_multipart_file(request)
            sid = self._next_id("skill")
            vid = self._next_id("skillver")
            grants = json.loads(fields.get("visibility_grants", "[]"))
            if "travel-expense" in pkg_filename:
                pkg_slug = "travel-expense-audit-pack"
                pkg_name = "差旅报销超标与备用金稽核审计包"
                pkg_desc = "SK-7"
            else:
                pkg_slug = "hroa-lifecycle-audit-pack"
                pkg_name = "入转调离月度风控稽核台账包"
                pkg_desc = "SK-5"
            record = {
                "id": sid,
                "slug": pkg_slug,
                "scope": fields.get("scope", "company"),
                "owner_id": actor,
                "owner_department_id": fields.get("owner_department_id"),
                "visibility_grants": grants,
                "name": pkg_name,
                "description": pkg_desc,
                "current_version_id": vid,
                "enabled": False,
                "script_requires_approval": False,
                "revision": 1,
                "format": "package",
                "featured": False,
                "starter_prompt": None,
                "versions": [
                    {
                        "id": vid,
                        "name": pkg_name,
                        "description": pkg_desc,
                        "guide": "",
                        "package_sha256": hashlib.sha256(pkg_bytes).hexdigest(),
                    }
                ],
            }
            self.skills[sid] = record
            return httpx.Response(201, json=record)

        if path.startswith("/api/v1/admin/skills/"):
            skill_parts = path.removeprefix("/api/v1/admin/skills/").split("/")
            sid = skill_parts[0]
            skill = self.skills[sid]
            if len(skill_parts) == 1 and method == "GET":
                return httpx.Response(200, json=skill)
            if (
                len(skill_parts) == 3
                and skill_parts[1] == "versions"
                and skill_parts[2] == "markdown"
                and method == "POST"
            ):
                body = json.loads(request.content)
                idem_key = str(request.headers.get("idempotency-key", "")).strip()
                if not idem_key or len(idem_key) > 128:
                    return httpx.Response(422, json={"detail": "idempotency_key_invalid"})
                fingerprint = json.dumps(body, ensure_ascii=False, sort_keys=True)
                slot = (actor, f"admin_md:{sid}", idem_key)
                if slot in self.skill_idem:
                    prev_fp, prev_resp = self.skill_idem[slot]
                    if prev_fp != fingerprint:
                        return httpx.Response(409, json={"detail": "idempotency_conflict"})
                    return httpx.Response(201, json=prev_resp)
                if int(body["expected_revision"]) != int(skill["revision"]):
                    return httpx.Response(409, json={"detail": "skill_revision_conflict"})
                vid = self._next_id("skillver")
                new_ver = {
                    "id": vid,
                    "name": body["name"],
                    "description": body["description"],
                    "guide": body["guide"],
                    "package_sha256": "",
                }
                cast(list[dict[str, Any]], skill["versions"]).insert(0, new_ver)
                skill["name"] = body["name"]
                skill["description"] = body["description"]
                if body.get("activate", True):
                    skill["current_version_id"] = vid
                skill["revision"] = int(skill["revision"]) + 1
                self.skill_idem[slot] = (fingerprint, dict(skill))
                return httpx.Response(201, json=skill)
            if len(skill_parts) == 1 and method == "PATCH":
                body = json.loads(request.content)
                if int(body["expected_revision"]) != int(skill["revision"]):
                    return httpx.Response(409, json={"detail": "skill_revision_conflict"})
                skill["enabled"] = body["enabled"]
                skill["script_requires_approval"] = body["script_requires_approval"]
                if "featured" in body and body["featured"] is not None:
                    skill["featured"] = body["featured"]
                if "starter_prompt" in body:
                    skill["starter_prompt"] = body["starter_prompt"]
                if "visibility_grants" in body and body["visibility_grants"] is not None:
                    skill["visibility_grants"] = body["visibility_grants"]
                skill["revision"] = int(skill["revision"]) + 1
                return httpx.Response(200, json=skill)

        if path == "/api/v1/me/skills":
            if method == "GET":
                mine = [
                    s
                    for s in self.skills.values()
                    if s["scope"] == "personal" and s["owner_id"] == actor
                ]
                return httpx.Response(200, json={"items": mine})
            if method == "POST":
                body = json.loads(request.content)
                sid = self._next_id("skill")
                vid = self._next_id("skillver")
                record = {
                    "id": sid,
                    "slug": f"personal-{sid}",
                    "scope": "personal",
                    "owner_id": actor,
                    "owner_department_id": None,
                    "visibility_grants": [],
                    "name": body["name"],
                    "description": body["description"],
                    "current_version_id": vid,
                    "enabled": True,
                    "script_requires_approval": False,
                    "revision": 1,
                    "format": "markdown",
                    "featured": False,
                    "starter_prompt": None,
                    "versions": [
                        {
                            "id": vid,
                            "name": body["name"],
                            "description": body["description"],
                            "guide": body["guide"],
                        }
                    ],
                }
                self.skills[sid] = record
                return httpx.Response(201, json=record)

        if path.startswith("/api/v1/me/skills/"):
            me_parts = path.removeprefix("/api/v1/me/skills/").split("/")
            sid = me_parts[0]
            skill = self.skills[sid]
            if len(me_parts) == 1 and method == "GET":
                return httpx.Response(200, json=skill)
            if len(me_parts) == 2 and me_parts[1] == "versions" and method == "POST":
                body = json.loads(request.content)
                idem_key = str(request.headers.get("idempotency-key", "")).strip()
                if not idem_key or len(idem_key) > 128:
                    return httpx.Response(422, json={"detail": "idempotency_key_invalid"})
                fingerprint = json.dumps(body, ensure_ascii=False, sort_keys=True)
                slot = (actor, f"personal:{sid}", idem_key)
                if slot in self.skill_idem:
                    prev_fp, prev_resp = self.skill_idem[slot]
                    if prev_fp != fingerprint:
                        return httpx.Response(409, json={"detail": "idempotency_conflict"})
                    return httpx.Response(201, json=prev_resp)
                if int(body["expected_revision"]) != int(skill["revision"]):
                    return httpx.Response(409, json={"detail": "skill_revision_conflict"})
                vid = self._next_id("skillver")
                new_ver = {
                    "id": vid,
                    "name": body["name"],
                    "description": body["description"],
                    "guide": body["guide"],
                }
                cast(list[dict[str, Any]], skill["versions"]).insert(0, new_ver)
                skill["name"] = body["name"]
                skill["description"] = body["description"]
                skill["current_version_id"] = vid
                skill["revision"] = int(skill["revision"]) + 1
                self.skill_idem[slot] = (fingerprint, dict(skill))
                return httpx.Response(201, json=skill)

        if path == "/api/v1/skills" and method == "GET":
            visible_skills = [
                s
                for s in self.skills.values()
                if s["enabled"] and self._can_read_resource(actor, s)
            ]
            # 与 SkillService.user_directory 一致：推荐置顶优先，其后部门 > 公司 > 个人。
            scope_rank = {"department": 0, "company": 1, "personal": 2}
            visible_skills.sort(
                key=lambda item: (
                    0 if item.get("featured") else 1,
                    scope_rank.get(str(item.get("scope")), 9),
                    str(item.get("slug")),
                )
            )
            return httpx.Response(200, json={"items": visible_skills})

        # 6. Subagent 管理员接口（含 offset/limit 分页、乐观锁 version 与 available-tools）。
        if path == "/api/v1/admin/subagents/available-tools" and method == "GET":
            if self._users[actor]["role"] != "super_admin":
                return httpx.Response(403, json={"detail": "admin_required"})
            builtin_items = [
                {
                    # 对齐真实后端 builtin 工具 ID 格式（如 builtin_knowledge_search）。
                    "tool_id": f"builtin_{name}",
                    "name": name,
                    "description": name,
                    "source_type": "builtin",
                    "requires_approval": False,
                    "server_id": None,
                    "server_name": None,
                }
                for name in (
                    "request_user_input",
                    "session_file_list",
                    "session_file_read",
                    "knowledge_search",
                    "skill_load",
                    "skill_read_resource",
                    "skill_execute",
                )
            ]
            mcp_items = [
                {
                    "tool_id": tool["tool_id"],
                    "name": tool["model_name"],
                    "description": tool["description"],
                    "source_type": "mcp",
                    "requires_approval": tool["requires_approval"],
                    "server_id": tool["server_id"],
                    "server_name": self.mcp_servers[tool["server_id"]]["name"],
                }
                for tool in self.mcp_tools.values()
            ]
            groups = [{"group_key": "builtin", "label": "内置工具", "items": builtin_items}]
            if mcp_items:
                groups.append({"group_key": "mcp", "label": "MCP 工具", "items": mcp_items})
            return httpx.Response(200, json={"groups": groups})

        if path == "/api/v1/admin/subagents":
            if self._users[actor]["role"] != "super_admin":
                return httpx.Response(403, json={"detail": "admin_required"})
            if method == "GET":
                offset = int(request.url.params.get("offset", "0"))
                limit = int(request.url.params.get("limit", "20"))
                all_items = list(self.subagents.values())
                sliced = all_items[offset : offset + limit]
                return httpx.Response(
                    200,
                    json={
                        "items": sliced,
                        "total": len(all_items),
                        "offset": offset,
                        "limit": limit,
                    },
                )
            if method == "POST":
                body = json.loads(request.content)
                allowed_keys = {
                    "slug",
                    "name",
                    "description",
                    "system_prompt",
                    "model_profile_id",
                    "allowed_tool_ids",
                    "enabled",
                    "max_llm_requests",
                    "timeout_seconds",
                    "visibility_scope",
                    "owner_department_id",
                    "visibility_grants",
                }
                if set(body) - allowed_keys:
                    return httpx.Response(422, json={"detail": "extra_forbidden"})
                sub_id = self._next_id("subagent")
                record = {
                    "id": sub_id,
                    "slug": body["slug"],
                    "name": body["name"],
                    "description": body["description"],
                    "system_prompt": body["system_prompt"],
                    "model_profile_id": body.get("model_profile_id"),
                    "allowed_tool_ids": list(body.get("allowed_tool_ids", [])),
                    "enabled": bool(body.get("enabled", True)),
                    "visibility_scope": body.get("visibility_scope", "company"),
                    "owner_department_id": body.get("owner_department_id"),
                    "visibility_grants": body.get("visibility_grants") or [],
                    "max_llm_requests": body.get("max_llm_requests"),
                    "timeout_seconds": body.get("timeout_seconds"),
                    "version": 1,
                    "created_by": actor,
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                }
                self.subagents[sub_id] = record
                return httpx.Response(201, json=record)

        if path.startswith("/api/v1/admin/subagents/"):
            if self._users[actor]["role"] != "super_admin":
                return httpx.Response(403, json={"detail": "admin_required"})
            sub_parts = path.removeprefix("/api/v1/admin/subagents/").split("/")
            sub_id = sub_parts[0]
            sub = self.subagents.get(sub_id)
            if sub is None:
                return httpx.Response(404, json={"detail": "subagent_not_found"})
            if len(sub_parts) == 1 and method == "PATCH":
                body = json.loads(request.content)
                allowed_patch_keys = {
                    "version",
                    "slug",
                    "name",
                    "description",
                    "system_prompt",
                    "model_profile_id",
                    "allowed_tool_ids",
                    "max_llm_requests",
                    "timeout_seconds",
                    "visibility_scope",
                    "owner_department_id",
                    "visibility_grants",
                }
                if set(body) - allowed_patch_keys:
                    return httpx.Response(422, json={"detail": "extra_forbidden"})
                if int(body["version"]) != int(sub["version"]):
                    return httpx.Response(409, json={"detail": "subagent_version_conflict"})
                for field in (
                    "slug",
                    "name",
                    "description",
                    "system_prompt",
                    "model_profile_id",
                    "allowed_tool_ids",
                    "max_llm_requests",
                    "timeout_seconds",
                    "visibility_scope",
                    "owner_department_id",
                ):
                    if field in body:
                        sub[field] = body[field]
                if "visibility_grants" in body:
                    sub["visibility_grants"] = body["visibility_grants"] or []
                sub["version"] = int(sub["version"]) + 1
                return httpx.Response(200, json=sub)
            if len(sub_parts) == 2 and sub_parts[1] == "enabled" and method == "PUT":
                body = json.loads(request.content)
                sub["enabled"] = bool(body["enabled"])
                sub["version"] = int(sub["version"]) + 1
                return httpx.Response(200, json=sub)

        # 7. Project 目录与项目知识库绑定接口。
        if path == "/api/v1/projects":
            if method == "GET":
                mine = [p for p in self.projects.values() if p["owner_id"] == actor]
                return httpx.Response(
                    200, json={"items": mine, "total": len(mine), "page": 1, "page_size": 100}
                )
            if method == "POST":
                body = json.loads(request.content)
                pid = self._next_id("proj")
                proj = {
                    "id": pid,
                    "owner_id": actor,
                    "name": body["name"],
                    "description": body.get("description"),
                    "instructions": body.get("instructions"),
                    "default_skill_ids": list(body.get("default_skill_ids", [])),
                    "created_at": "2026-11-24T00:00:00Z",
                    "updated_at": "2026-11-24T00:00:00Z",
                }
                self.projects[pid] = proj
                self.project_kb_bindings[pid] = []
                return httpx.Response(201, json=proj)

        if path.startswith("/api/v1/projects/"):
            p_parts = path.removeprefix("/api/v1/projects/").split("/")
            pid = p_parts[0]
            target_proj = self.projects.get(pid)
            if target_proj is None or target_proj["owner_id"] != actor:
                return httpx.Response(404, json={"detail": "project_not_found"})
            if len(p_parts) == 1 and method == "PATCH":
                body = json.loads(request.content)
                for field in ("name", "description", "instructions", "default_skill_ids"):
                    if field in body:
                        target_proj[field] = body[field]
                return httpx.Response(200, json=target_proj)
            if len(p_parts) == 2 and p_parts[1] == "knowledge-bases" and method == "GET":
                bound = [
                    self.kbs[kb_id]
                    for kb_id in self.project_kb_bindings.get(pid, [])
                    if kb_id in self.kbs and self._can_read_resource(actor, self.kbs[kb_id])
                ]
                return httpx.Response(200, json=bound)
            if len(p_parts) == 3 and p_parts[1] == "knowledge-bases" and method == "PUT":
                kb_id = p_parts[2]
                kb = self.kbs.get(kb_id)
                if kb is None or not self._can_read_resource(actor, kb):
                    return httpx.Response(404, json={"detail": "knowledge_base_not_found"})
                kb_bucket = self.project_kb_bindings.setdefault(pid, [])
                if kb_id not in kb_bucket:
                    kb_bucket.append(kb_id)
                return httpx.Response(204)

        return httpx.Response(404, json={"detail": f"unhandled:{method}:{path}"})


def _query_tokens(query: str) -> list[str]:
    """为离线 Mock 检索切分高区分度关键词（支持制度编号、工号及中文核心短语）。"""
    import re

    ascii_tokens = re.findall(r"[A-Za-z0-9_.\-]{2,}", query)
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    tokens: list[str] = list(ascii_tokens)
    for run in cjk_runs:
        if len(run) <= 4:
            tokens.append(run)
        else:
            for idx in range(0, len(run) - 1):
                tokens.append(run[idx : idx + 2])
    return list(dict.fromkeys(tokens))


def test_seed_enterprise_data_dry_run_idempotency_and_verify(tmp_path: Path) -> None:
    """验证干跑零写请求、首次 V3 全量导入（36 档）、幂等重跑零写及 110 题量化评测报告落盘。"""
    backend = _SimulatedAthenaBackend()
    report_path = tmp_path / "reports" / "enterprise_eval_report.json"

    # 1. --dry-run 必须发出零 HTTP 请求且不写出评测报告。
    dry_seeder = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        dry_run=True,
        eval_report_path=report_path,
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        dry_seeder.seed_knowledge()
        dry_seeder.seed_memories()
        dry_seeder.seed_mcp()
        dry_seeder.seed_skills()
        dry_seeder.seed_subagents()
        dry_seeder.seed_projects()
        dry_seeder.verify_all()
    finally:
        dry_seeder.close()
    assert backend.requests == []
    assert not report_path.exists()

    # 2. 首次执行 V3 全量导入（6 库 36 档、3 MCP/16 工具、9 技能、3 Subagents、2 项目）。
    first = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        poll_interval=0.0,
        eval_report_path=report_path,
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        first.seed_knowledge()
        first.seed_memories()
        first.seed_mcp()
        first.seed_skills()
        first.seed_subagents()
        first.seed_projects()
        first.verify_all()
    finally:
        first.close()

    assert first.summary.created == {
        "knowledge_bases": 6,
        "documents": 36,
        "document_versions": 0,
        "memories": 16,
        "mcp_servers": 3,
        "skills": 9,
        "subagents": 3,
        "projects": 2,
        "project_kb_bindings": 5,
    }
    assert first.summary.updated["preferences"] == 6
    assert first.summary.updated["mcp_tools"] == 16
    assert first.summary.verified_checks == [
        "knowledge_bases_and_documents_ready",
        "rbac_403_404_matrix",
        "mcp_visibility_and_approval_metadata",
        "skills_visibility_and_featured_order",
        "subagents_configured_and_tool_whitelists",
        "projects_and_knowledge_bindings",
        "preferences_and_memories_baseline",
        "enterprise_retrieval_eval_positive_and_confusable",
        "enterprise_retrieval_eval_and_rbac_zero_leak",
    ]
    assert len(first.summary.verified_checks) == 9

    # 校验 110 题量化评测报告完整挂载并精准落盘至 --eval-report 路径。
    report = first.summary.retrieval_eval_report
    assert report is not None
    assert report["total_cases"] == 110
    assert report["positive_cases"] == 93
    assert report["rbac_cases"] == 13
    assert report["rbac_leak_count"] == 0
    assert report["hit_at_5"] >= 0.90
    assert report["hit_at_8"] >= 0.95
    assert report["by_kind"]["exact_policy_code"]["hit_at_5"] == 1.0
    assert report["by_kind"]["identifier_entity"]["hit_at_5"] == 1.0
    assert 0.0 <= report["hit_at_1"] <= 1.0
    assert 0.0 <= report["mrr"] <= 1.0
    assert 0.0 <= report["hard_negative_top1_win_rate"] <= 1.0
    assert len(report["cases"]) == 93
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8")) == report

    # 3. 幂等重跑：不得发起任何重复 POST/PUT/PATCH 创建或修改资源，且保留既有 chat_explicit 记忆。
    requests_before_second = len(backend.requests)
    second = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        poll_interval=0.0,
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        second.seed_knowledge()
        second.seed_memories()
        second.seed_mcp()
        second.seed_skills()
        second.seed_subagents()
        second.seed_projects()
    finally:
        second.close()

    second_requests = backend.requests[requests_before_second:]
    mutating_calls = [
        (m, p)
        for m, p in second_requests
        if m in {"POST", "PATCH", "PUT", "DELETE"} and p != "/api/v1/auth/mock/login"
    ]
    assert mutating_calls == [], f"idempotent_rerun_must_not_mutate:{mutating_calls}"
    assert all(count == 0 for count in second.summary.created.values())
    assert all(count == 0 for count in second.summary.updated.values())

    # 确认 mock_hr_member 对话中显式保存的 chat_explicit 记忆毫发无损。
    hr_memories = backend.memories["mock_hr_member"]
    assert any(item["source"] == "chat_explicit" for item in hr_memories)

    # 校验 manifest.yaml 与模拟后端数量完全对齐。
    manifest = cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))
    assert len(backend.kbs) == len(manifest["knowledge_bases"])
    assert len(backend.mcp_servers) == len(manifest["mcp_servers"])
    assert len(backend.skills) == len(manifest["skills"])
    assert len(backend.subagents) == len(manifest["subagents"])
    assert len(backend.projects) == len(manifest["projects"])


def test_doc01_v1_v2_roundtrip_restores_baseline_every_time() -> None:
    """验证 DOC-01 反复执行 V1->V2->V1->V2->V1 往返替换时，每次恢复基线都真实发布新版本。"""
    backend = _SimulatedAthenaBackend()
    v1_bytes = (GENERATED_DIR / "kb/DOC-01_XC-HR-2026-001_假勤休假管理办法.md").read_bytes()
    v2_bytes = (GENERATED_DIR / "kb/DOC-01_XC-HR-2026-001-REV2_假勤休假管理办法_v2.md").read_bytes()

    def run_seed_kb() -> EnterpriseSeeder:
        seeder = EnterpriseSeeder(
            base_url="http://mock.athena.local",
            poll_interval=0.0,
            transport_factory=lambda: httpx.MockTransport(backend.handle),
        )
        try:
            seeder.seed_knowledge()
            return seeder
        finally:
            seeder.close()

    # 1. 首发导入 V1。
    initial = run_seed_kb()
    kb1_id = initial.summary.kb_ids_by_key["kb_company_policies"]
    doc1_id = initial.summary.doc_ids_by_code["DOC-01"]
    assert backend.doc_sources[backend.docs[doc1_id]["active_version_id"]] == v1_bytes

    # 2. 连续两轮模拟测试升级为 V2 后再用导入器恢复 V1，验证绝不因旧幂等槽静默残留在 V2。
    for round_no in (1, 2):
        admin_client = httpx.Client(
            base_url="http://mock.athena.local",
            headers={"Origin": "http://mock.athena.local"},
            transport=httpx.MockTransport(backend.handle),
        )
        try:
            assert (
                admin_client.post(
                    "/api/v1/auth/mock/login", json={"user_id": "mock_super_admin"}
                ).status_code
                == 200
            )
            up_v2 = admin_client.post(
                f"/api/v1/knowledge-bases/{kb1_id}/documents/{doc1_id}/versions/upload",
                headers={"Idempotency-Key": f"manual-upgrade-v2-round-{round_no}"},
                files={
                    "file": (
                        "DOC-01_XC-HR-2026-001-REV2_假勤休假管理办法_v2.md",
                        v2_bytes,
                        "text/markdown",
                    )
                },
            )
            assert up_v2.status_code == 202
        finally:
            admin_client.close()
        assert backend.doc_sources[backend.docs[doc1_id]["active_version_id"]] == v2_bytes

        restored = run_seed_kb()
        assert restored.summary.created["document_versions"] == 1
        assert backend.doc_sources[backend.docs[doc1_id]["active_version_id"]] == v1_bytes


def test_skills_dirty_and_restore_roundtrip_avoids_409_idempotency_conflict() -> None:
    """验证管理端 Markdown（SK-1）与个人 Skill（SK-6）多次改脏并恢复基线均零 409 冲突。"""
    backend = _SimulatedAthenaBackend()

    def run_seed_skills() -> EnterpriseSeeder:
        seeder = EnterpriseSeeder(
            base_url="http://mock.athena.local",
            poll_interval=0.0,
            transport_factory=lambda: httpx.MockTransport(backend.handle),
        )
        try:
            seeder.seed_skills()
            return seeder
        finally:
            seeder.close()

    run_seed_skills()
    sk1 = next(s for s in backend.skills.values() if s["slug"] == "hr-leave-assistant")
    sk6 = next(s for s in backend.skills.values() if s["scope"] == "personal")
    orig_sk1_guide = sk1["versions"][0]["guide"]
    orig_sk6_guide = sk6["versions"][0]["guide"]

    # 连续两轮模拟“手工改脏 -> 重跑脚本恢复”，验证 expected_revision 编入幂等键避免 409。
    for round_no in (1, 2):
        sk1["versions"][0]["guide"] = f"被手工改脏的 SK-1 正文 round={round_no}"
        sk6["versions"][0]["guide"] = f"被手工改脏的 SK-6 正文 round={round_no}"

        restored = run_seed_skills()
        assert restored.summary.updated["skills"] == 2
        assert sk1["versions"][0]["guide"] == orig_sk1_guide
        assert sk6["versions"][0]["guide"] == orig_sk6_guide


def test_reset_memories_rebuilds_manual_and_preserves_chat_explicit() -> None:
    """验证 --reset-memories 仅清理 manual 来源记忆并重建基线，绝不误删 chat_explicit 记忆。"""
    backend = _SimulatedAthenaBackend()
    backend.memories["mock_hr_member"].append(
        {
            "id": "umem_stale_manual",
            "content": "过期的旧手动记忆，应在 --reset-memories 时被清理。",
            "source": "manual",
            "created_at": "2026-11-24T08:00:00Z",
            "updated_at": "2026-11-24T08:00:00Z",
        }
    )

    seeder = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        reset_memories=True,
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        seeder.seed_memories()
    finally:
        seeder.close()

    hr_items = backend.memories["mock_hr_member"]
    assert any(
        item["id"] == "umem_chat_keep_1" and item["source"] == "chat_explicit" for item in hr_items
    )
    assert all(item["id"] != "umem_stale_manual" for item in hr_items)
    assert sum(1 for item in hr_items if item["source"] == "manual") == 3


def test_reset_hroa_state_guard_for_docker_and_host(tmp_path: Path) -> None:
    """验证 --reset-hroa-state 在宿主机 Docker 模式拒绝误写 /app/var，支持容器与宿主机模式。"""
    # 1. 宿主机 + docker 模式未传 --docker-container 时必须明确报错，绝不在宿主机写 /app/var。
    with pytest.raises(RuntimeError, match="hroa_reset_requires_docker_container"):
        reset_hroa_state_safely(
            mcp_runtime="docker",
            state_file=None,
            docker_container=None,
            dry_run=False,
            in_container=False,
        )

    # 2. 宿主机 + docker 模式传入 --docker-container 时，安全拼接列表命令执行 docker exec。
    captured_cmds: list[list[str]] = []

    def fake_runner(cmd: list[str], *, check: bool = False) -> None:
        assert check is True
        captured_cmds.append(cmd)

    docker_res = reset_hroa_state_safely(
        mcp_runtime="docker",
        state_file=None,
        docker_container="athena-backend-api",
        dry_run=False,
        in_container=False,
        runner=fake_runner,
    )
    assert docker_res["mode"] == "docker_exec"
    assert captured_cmds == [
        [
            "docker",
            "exec",
            "athena-backend-api",
            "/usr/local/bin/python",
            "/app/mcp_servers/hroa/mcp_server.py",
            "--state-file",
            "/app/var/hroa-test/state.json",
            "--reset-state",
        ]
    ]

    # 2b. 显式传入 --docker-container 且路径非 /app/ 前缀时，依然统一走 docker exec。
    captured_cmds.clear()
    custom_docker_res = reset_hroa_state_safely(
        mcp_runtime="docker",
        state_file="/tmp/custom-hroa/state.json",
        docker_container="athena-backend-api",
        dry_run=False,
        in_container=False,
        runner=fake_runner,
    )
    assert custom_docker_res["mode"] == "docker_exec"
    assert captured_cmds == [
        [
            "docker",
            "exec",
            "athena-backend-api",
            "/usr/local/bin/python",
            "/app/mcp_servers/hroa/mcp_server.py",
            "--state-file",
            "/tmp/custom-hroa/state.json",
            "--reset-state",
        ]
    ]

    # 3. host 模式可直接在本地临时状态文件执行复位，将变更清零回 2026-11-24 基线。
    host_state = tmp_path / "host-state.json"
    store = HroaStateStore(host_state)
    store.submit_leave_request(
        employee_no="MOCK005",
        employee_name="陆知微",
        leave_type="annual",
        start_date="2026-12-07",
        end_date="2026-12-07",
        days=1.0,
        reason="待复位变更",
        handover_to="MOCK003",
    )
    assert store.get_leave_balance(employee_no="MOCK005")["annual_remaining_days"] == 3.5

    reset_hroa_state_safely(
        mcp_runtime="host",
        state_file=str(host_state),
        docker_container=None,
        dry_run=False,
        in_container=False,
    )
    assert store.get_leave_balance(employee_no="MOCK005")["annual_remaining_days"] == 4.5


def test_doc06_and_doc29_ocr_failure_warns_without_blocking_seed_or_verify() -> None:
    """验证 DOC-06 与 DOC-29（两份 OCR 流程图）失败时仅记告警，不阻断其余 34 档导入与全量验收。"""
    backend = _SimulatedAthenaBackend(fail_ocr_uploads=True)
    seeder = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        poll_interval=0.0,
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        seeder.seed_knowledge()
        seeder.seed_memories()
        seeder.seed_mcp()
        seeder.seed_skills()
        seeder.seed_subagents()
        seeder.seed_projects()
        seeder.verify_all()
    finally:
        seeder.close()

    assert seeder.summary.created["documents"] == 36
    assert any("DOC-06" in w for w in seeder.summary.warnings)
    assert any("DOC-29" in w for w in seeder.summary.warnings)
    assert "knowledge_bases_and_documents_ready" in seeder.summary.verified_checks
    assert "enterprise_retrieval_eval_and_rbac_zero_leak" in seeder.summary.verified_checks
    assert len(seeder.summary.verified_checks) == 9


def test_verify_org_lifecycle_disables_revokes_cookie_and_restores_active() -> None:
    """验证 --verify-org-lifecycle 停用 mock_fin_member 使原 Cookie 401 并在 finally 恢复。"""
    backend = _SimulatedAthenaBackend()
    seeder = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        poll_interval=0.0,
        verify_org_lifecycle=True,
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        seeder.seed_knowledge()
        seeder.seed_memories()
        seeder.seed_mcp()
        seeder.seed_skills()
        seeder.seed_subagents()
        seeder.seed_projects()
        seeder.verify_all()
    finally:
        seeder.close()

    assert "org_lifecycle_disable_revokes_session_and_restores" in seeder.summary.verified_checks
    assert backend.user_status["mock_fin_member"] == "active"
    status_calls = [
        (m, p)
        for m, p in backend.requests
        if m == "PUT" and p == "/api/v1/admin/org/users/mock_fin_member/status"
    ]
    assert len(status_calls) == 2


def test_doc13_v1_v2_roundtrip_restores_baseline() -> None:
    """验证第 2 份版本替换样本 DOC-13-V2（7 工作日申诉窗口）升级后可被导入器无损恢复 V1 基线。"""
    backend = _SimulatedAthenaBackend()
    v1_bytes = (GENERATED_DIR / "kb/DOC-13_XC-HR-2026-007_绩效考核与申诉制度.md").read_bytes()
    v2_bytes = (
        GENERATED_DIR / "kb/DOC-13_XC-HR-2026-007-REV2_绩效考核与申诉制度_v2.md"
    ).read_bytes()

    def run_seed_kb() -> EnterpriseSeeder:
        seeder = EnterpriseSeeder(
            base_url="http://mock.athena.local",
            poll_interval=0.0,
            transport_factory=lambda: httpx.MockTransport(backend.handle),
        )
        try:
            seeder.seed_knowledge()
            return seeder
        finally:
            seeder.close()

    initial = run_seed_kb()
    kb1_id = initial.summary.kb_ids_by_key["kb_company_policies"]
    doc13_id = initial.summary.doc_ids_by_code["DOC-13"]
    assert backend.doc_sources[backend.docs[doc13_id]["active_version_id"]] == v1_bytes

    admin_client = httpx.Client(
        base_url="http://mock.athena.local",
        headers={"Origin": "http://mock.athena.local"},
        transport=httpx.MockTransport(backend.handle),
    )
    try:
        assert (
            admin_client.post(
                "/api/v1/auth/mock/login", json={"user_id": "mock_super_admin"}
            ).status_code
            == 200
        )
        up_v2 = admin_client.post(
            f"/api/v1/knowledge-bases/{kb1_id}/documents/{doc13_id}/versions/upload",
            headers={"Idempotency-Key": "manual-upgrade-doc13-v2"},
            files={
                "file": (
                    "DOC-13_XC-HR-2026-007-REV2_绩效考核与申诉制度_v2.md",
                    v2_bytes,
                    "text/markdown",
                )
            },
        )
        assert up_v2.status_code == 202
    finally:
        admin_client.close()
    assert backend.doc_sources[backend.docs[doc13_id]["active_version_id"]] == v2_bytes

    restored = run_seed_kb()
    assert restored.summary.created["document_versions"] == 1
    assert backend.doc_sources[backend.docs[doc13_id]["active_version_id"]] == v1_bytes


def test_subagent_and_project_drift_repairs_via_optimistic_lock_and_patch() -> None:
    """验证 Subagent 漂移经 version 乐观锁 PATCH + PUT 对齐，Project 漂移经 PATCH 修复。"""
    backend = _SimulatedAthenaBackend()

    def seed_all() -> EnterpriseSeeder:
        seeder = EnterpriseSeeder(
            base_url="http://mock.athena.local",
            mcp_runtime="docker",
            poll_interval=0.0,
            transport_factory=lambda: httpx.MockTransport(backend.handle),
        )
        try:
            seeder.seed_knowledge()
            seeder.seed_mcp()
            seeder.seed_skills()
            seeder.seed_subagents()
            seeder.seed_projects()
            return seeder
        finally:
            seeder.close()

    seed_all()
    sub = next(s for s in backend.subagents.values() if s["slug"] == "hr-policy-auditor")
    sub_fin = next(s for s in backend.subagents.values() if s["slug"] == "finance-travel-auditor")
    proj = next(p for p in backend.projects.values() if p["owner_id"] == "mock_hr_member")

    # 校验模拟后端解析后的内置工具白名单使用真实 builtin_<name> 格式。
    assert "builtin_knowledge_search" in sub["allowed_tool_ids"]
    assert "builtin_session_file_read" in sub_fin["allowed_tool_ids"]

    # 模拟手工篡改 Subagent 名称/停用状态、SUB-2 跨部门 visibility_grants 以及 Project 字段。
    sub["name"] = "被修改的名称"
    sub["enabled"] = False
    sub_fin["visibility_grants"] = [
        {"target_type": "department", "target_id": "dept_mock_fin", "include_subdepartments": True}
    ]
    proj["instructions"] = "过期的项目指令"
    proj["default_skill_ids"] = []

    repaired = seed_all()
    assert repaired.summary.created["subagents"] == 0
    assert repaired.summary.updated["subagents"] == 2
    assert repaired.summary.created["projects"] == 0
    assert repaired.summary.updated["projects"] == 1
    assert sub["name"] == "HR 制度与假勤绩效核验专员"
    assert sub["enabled"] is True
    assert int(sub["version"]) == 3  # PATCH (+1) + PUT enabled (+1)
    repaired_grant_depts = {g["target_id"] for g in sub_fin["visibility_grants"]}
    assert repaired_grant_depts == {"dept_mock_fin", "dept_mock_hr"}
    assert int(sub_fin["version"]) == 2
    assert len(proj["default_skill_ids"]) == 2


def test_mcp_server_config_drift_uses_patch_and_rediscover() -> None:
    """验证 MCP Server 配置漂移时通过 PATCH + rediscover 对齐而不重复新建，且无损保留 tool_id。"""
    backend = _SimulatedAthenaBackend()
    first = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        first.seed_mcp()
    finally:
        first.close()

    original_tool_ids = set(backend.mcp_tools)
    std_server = next(s for s in backend.mcp_servers.values() if s["name"] == "hroa-standard")
    std_server["config"]["args"] = ["/app/mcp_servers/hroa/mcp_server.py", "--profile", "standard"]
    std_server["enabled"] = False

    repaired = EnterpriseSeeder(
        base_url="http://mock.athena.local",
        mcp_runtime="docker",
        transport_factory=lambda: httpx.MockTransport(backend.handle),
    )
    try:
        repaired.seed_mcp()
    finally:
        repaired.close()

    assert repaired.summary.created["mcp_servers"] == 0
    assert repaired.summary.updated["mcp_servers"] == 1
    assert std_server["enabled"] is True
    assert int(std_server["config_version"]) == 2
    assert set(backend.mcp_tools) == original_tool_ids
