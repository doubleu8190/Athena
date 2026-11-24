# Athena v2 第三期多模态与技能双形态后端 API 契约文档

> **适用里程碑**：第三期后端 M3.1（文档提取与安全预览）、M3.2（VLM 多模态视觉输入与方案 A2 降级）、M3.3（公共/部门技能在线 Markdown 双形态与首页快捷应用元数据）
> **数据库基线**：Alembic 唯一 head `20261206_0039`（依次承接 `20261205_0038` ← `20261204_0037`）
> **面向读者**：M3.4–M3.6 前端实施与 QA 联调人员（本文件为第三期前后端联调唯一权威契约）
>
> **全局通用约定**：
> - **认证方式**：服务端可撤销 HttpOnly 同站 Cookie（`athena_session`，`SameSite=Lax`）。未登录访问非公开 `/api/v1/*` 路由统一由前置中间件返回 `401 {"detail": "not_authenticated"}`。
> - **同源保护**：所有写请求（`POST` / `PATCH` / `DELETE`）如携带 `Origin` 请求头，必须严格同源，否则返回 `403 {"detail": "origin_not_allowed"}`。
> - **幂等请求头**：标注需要 `Idempotency-Key` 的写端点，仅接受 `1..128` 字符可见 ASCII（`^[\x21-\x7e]{1,128}$`），格式非法返回 `422 idempotency_key_invalid`，同键异参返回 `409 idempotency_conflict`。
> - **统一错误体**：常规业务错误返回 `{"detail": "<stable_error_code>"}`；Markdown 表单/解析类字段错误（复用个人技能校验体系）在具备安全字段定位时返回 `{"detail": {"code": "<stable_error_code>", "field": "<field_name>"}}`；包成员路径类错误返回 `{"detail": {"code": "<stable_error_code>", "member_path": "<safe_path>"}}`。后端绝不回显原始异常堆栈、文件内容或宿主路径。

---

## 目录

1. [M3.1 会话附件文本提取缓存与安全预览 / 读取接口](#一m31-会话附件文本提取缓存与安全预览--读取接口)
2. [M3.2 多模态视觉输入（VLM）、隐私剥离与方案 A2 降级语义](#二m32-多模态视觉输入vlm隐私剥离与方案-a2-降级语义)
3. [M3.3 公共 / 部门技能在线 Markdown 双形态与首页快捷应用元数据](#三m33-公共--部门技能在线-markdown-双形态与首页快捷应用元数据)
4. [第三期错误码速查与前端映射建议](#四第三期错误码速查与前端映射建议)

---

## 一、M3.1 会话附件文本提取缓存与安全预览 / 读取接口

### 1.1 `SessionFileResponse` 新增字段

以下全部接口返回的会话文件对象均统一携带 3 个提取状态字段（`POST /api/v1/sessions/{session_id}/files`、`GET /api/v1/sessions/{session_id}/files`、`GET /api/v1/sessions/{session_id}/files/{file_id}`，以及 `GET .../content` 中的 `file` 字段）：

```json
{
  "id": "file_01j9abc...",
  "session_id": "sess_01j9xyz...",
  "source": "upload",
  "filename": "2026年Q3财务报告.pdf",
  "label": null,
  "mime_type": "application/pdf",
  "size_bytes": 184320,
  "extraction_status": "ready",
  "extracted_text_bytes": 42910,
  "extraction_error_code": null,
  "created_at": "2026-11-24T05:00:00Z"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `extraction_status` | `"none" \| "ready" \| "failed"` | 提取缓存状态：`none`（非办公文档或历史未解析文件）、`ready`（已提取并缓存为 UTF-8 结构化文本）、`failed`（提取失败或超出安全上限，安全降级，不阻断原文件上传与下载） |
| `extracted_text_bytes` | `int \| null` | `extraction_status="ready"` 时必为非负整数（缓存 UTF-8 文本字节数）；其余状态必为 `null` |
| `extraction_error_code` | `string \| null` | `extraction_status="failed"` 时必为稳定安全错误码；其余状态必为 `null` |

> **数据库组合一致性不变式（`ck_session_files_extraction_consistency`）**：
> - `none`：`extracted_text_bytes = null` 且 `extraction_error_code = null`
> - `ready`：`extracted_text_bytes >= 0` 且 `extraction_error_code = null`
> - `failed`：`extracted_text_bytes = null` 且 `extraction_error_code != null`

### 1.2 自动提取范围与安全边界

- **支持自动提取的办公文档扩展名**：`.pdf`、`.docx`、`.xlsx`、`.pptx`（纯文本白名单 `.txt` / `.csv` / `.json` / `.md` / `.log` 直接读取原件，保持 `extraction_status="none"`）。
- **上传与惰性补解析**：
  - 用户上传上述 4 类文档时，后端在 15 秒超时边界内同步执行无 OCR 结构化纯文本提取（复用 `LocalDocumentParser(enable_ocr=False)`），生成带分节标头（`## 第 N 页` / `## 工作表：...` / `## 幻灯片 N` / `## 标题`）的 UTF-8 文本缓存至私有对象存储。
  - 若提取失败（如扫描版无文本 PDF、加密文档、含宏 Office 文档或超时），**文件上传本身仍返回 `201 Created`**，并将 `extraction_status="failed"` 与 `extraction_error_code` 落库，用户仍可下载原件或交由技能脚本处理。
  - 历史存量文件或技能脚本产出的 PDF / Office 产物（初始 `extraction_status="none"`）在首次调用 `GET .../content` 或 `session_file_read` 工具时自动惰性补解析并回填缓存。
- **硬性安全上限**：
  - 单文件上传上限：`50 MiB`（超限返回 `413 document_file_too_large`）；
  - 自动提取原文件上限：`20 MiB`（超限记录 `extraction_status="failed"`, `extraction_error_code="document_extract_too_large"`）；
  - 提取后 UTF-8 文本缓存上限：`10 MiB`（超限记录 `document_extracted_text_too_large`）；
  - PDF 页数上限 `500` 页、Office ZIP 成员数与解压体积受防炸弹限制、禁止 Office 宏与加密文档、提取超时 `15s`。

### 1.3 安全位图内联预览

- **端点**：`GET /api/v1/sessions/{session_id}/files/{file_id}/preview`
- **权限**：仅当前登录会话所有者可见（跨用户或不存在统一返回 `404 session_file_not_found`）。
- **安全白名单**：**仅放行 `image/png`、`image/jpeg`、`image/webp`**。HTML、SVG、PDF、JS、Office 及其他任意 MIME 一律拒绝，防止同源脚本执行（XSS）。
- **成功响应 `200 OK`**：
  - 响应体：二进制图片流（后端先预读首块确认对象可读后再发送 `200`，避免对象丢失导致截断流）；
  - 响应头：
    - `Content-Type`: 实际校验后的位图 MIME（`image/png` / `image/jpeg` / `image/webp`）
    - `Content-Disposition`: `inline; filename="..."; filename*=UTF-8''...`
    - `Content-Security-Policy`: `default-src 'none'; sandbox`
    - `X-Content-Type-Options`: `nosniff`
    - `Cache-Control`: `private, no-store`
    - `Content-Length`: `<size_bytes>`
- **错误响应**：

| HTTP 状态码 | `detail` | 触发条件 |
| --- | --- | --- |
| `401` | `"not_authenticated"` | 未登录或会话已过期 |
| `404` | `"session_file_not_found"` | 文件不存在、会话不存在或非当前用户所属会话 |
| `415` | `"file_preview_unsupported"` | 文件 MIME 不在 `image/png` / `image/jpeg` / `image/webp` 安全内联白名单内 |
| `503` | `"file_storage_unavailable"` | 私有对象存储不可用或底层读取失败 |

### 1.4 文本与提取文档分段读取

- **端点**：`GET /api/v1/sessions/{session_id}/files/{file_id}/content`
- **权限**：仅当前登录会话所有者可见；响应强制携带 `Cache-Control: private, no-store`。
- **Query 参数**：

| 参数 | 类型 | 默认值 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `offset` | `int` | `0` | `>= 0` | UTF-8 字节偏移量（续读时必须传入上一次响应的 `next_offset`，禁止落入多字节 UTF-8 字符中间） |
| `limit_bytes` | `int` | `65536` | `1..65536` | 本次最多读取的字节数（默认 64 KiB），后端自动回退至完整 UTF-8 字符边界 |

- **成功响应 `200 OK`（`SessionFileContentResponse`）**：

```json
{
  "file": {
    "id": "file_01j9abc...",
    "session_id": "sess_01j9xyz...",
    "source": "upload",
    "filename": "2026年Q3财务报告.pdf",
    "label": null,
    "mime_type": "application/pdf",
    "size_bytes": 184320,
    "extraction_status": "ready",
    "extracted_text_bytes": 42910,
    "extraction_error_code": null,
    "created_at": "2026-11-24T05:00:00Z"
  },
  "source_kind": "extracted_document",
  "offset": 0,
  "next_offset": 42910,
  "content": "## 第 1 页\n2026 年第三季度营收概要...",
  "eof": true,
  "truncated": false
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `file` | `SessionFileResponse` | 最新文件元数据（含惰性补解析后已更新的 `extraction_status` 与 `extracted_text_bytes`） |
| `source_kind` | `"raw_text" \| "extracted_document"` | `raw_text` 表示纯文本原件；`extracted_document` 表示 PDF / Office 自动提取缓存 |
| `offset` | `int` | 本次起始字节偏移 |
| `next_offset` | `int` | 下一次分页读取应传入的 `offset` |
| `content` | `string` | 严格合法 UTF-8 文本分片 |
| `eof` | `bool` | 是否已读到末尾 |
| `truncated` | `bool` | 是否仍有后续未读字节（`not eof`） |

- **错误响应**：

| HTTP 状态码 | `detail` | 触发条件 |
| --- | --- | --- |
| `401` | `"not_authenticated"` | 未登录 |
| `404` | `"session_file_not_found"` | 文件或会话不存在 / 非本人会话 |
| `415` | `"file_not_text"` | 非纯文本且非可提取办公文档（如图片、ZIP、音频等二进制文件） |
| `422` | `[{"loc": [...], "msg": "...", "type": "..."}]`（FastAPI 脱敏字段错误数组） | Query 参数校验失败（`offset < 0` 或 `limit_bytes` 超出 `1..65536`）；服务层 / 内置工具 `session_file_read` 边界校验则返回稳定错误码 `"session_file_range_invalid"` |
| `422` | `"file_offset_invalid"` | `offset > 总字节数`，或 `offset` 落在多字节 UTF-8 续字节中间 |
| `422` | `"file_content_not_text"` | 原文件包含非法 UTF-8 字节序列 |
| `422` | `"file_range_too_small"` | `limit_bytes` 小于当前位置单个多字节 UTF-8 字符长度导致零进度 |
| `422` | `"document_extract_too_large"` | 文档原体积超过 20 MiB 自动提取上限 |
| `422` | `"document_encrypted"` | PDF 或 Office 文档受密码加密保护 |
| `422` | `"document_macro_unsupported"` | Office 文档包含宏（VBA）被安全策略拒绝 |
| `422` | `"document_page_limit_exceeded"` | PDF 页数超过 500 页上限 |
| `422` | `"document_archive_limit_exceeded"` | Office OOXML 压缩包成员数或解压体积超限（防 ZIP 炸弹） |
| `422` | `"document_extracted_text_too_large"` | 提取出的文本超过 10 MiB 缓存上限或表格单元格超限 |
| `422` | `"document_no_extractable_text"` | 扫描版 PDF 或空白文档无可提取文本层（可引导用户转图片识图或下载原件） |
| `422` | `"document_type_unsupported"` | 不支持的旧版或未知办公格式（如 `.doc` / `.xls` / `.ppt`） |
| `422` | `"document_extract_timeout"` | 文档解析超过 15 秒超时上限 |
| `422` | `"document_parse_failed"` | 文档损坏或无法解析 |
| `503` | `"file_storage_unavailable"` | 对象存储不可用 |

> **模型侧联动**：内置工具 `session_file_read` 底层与 `GET .../content` 共用同一套读取与惰性提取缓存管线，因此对话中模型可直接分段阅读已上传的 PDF / DOCX / XLSX / PPTX 文档，无需人工转文本。

---

## 二、M3.2 多模态视觉输入（VLM）、隐私剥离与方案 A2 降级语义

### 2.1 模型识图能力声明与感知接口

前端不硬编码模型名判断识图能力，统一以服务端返回的 `supports_vision: boolean` 为准：

1. **普通用户模型目录**：`GET /api/v1/models` → `items[].supports_vision: bool`
2. **当前 Run 模型快照**：`RunResponse.model_snapshot.supports_vision: bool`（Run 创建时冻结，执行期间不受后台改配影响）
3. **超管模型配置管理**：`GET / POST / PATCH /api/v1/admin/llm/model-profiles` → `supports_vision: bool`

### 2.2 图片附件挂载方式（复用既有上传与 Run 提交契约）

无需新增多模态专用上传端点，复用两步标准流程：
1. 调用 `POST /api/v1/sessions/{session_id}/files`（带 `Idempotency-Key`）上传图片文件（PNG / JPEG / WEBP），获取 `SessionFileResponse.id`；前端可通过 `GET /api/v1/sessions/{session_id}/files/{file_id}/preview` 直接安全展示缩略图与灯箱大图。
2. 调用 `POST /api/v1/sessions/{session_id}/runs`（或澄清回复 `POST /api/v1/sessions/{session_id}/runs/{run_id}/input-requests/{input_request_id}/answer`）在 `input_file_ids` 数组中传入图片 `file_id`（可与 PDF / Office / 文本文件混合传入）。

### 2.3 图片安全边界、5 张上限与隐私剥离

当 Run 所选模型满足 `supports_vision = true` 时，Worker 在上下文构建前对本 Run 关联的图片附件执行一次有界加载与归一化（单次 Run 多轮工具调用内复用于内存缓存，绝不重复下载，且**图片 base64 绝不写入数据库、绝不写入执行日志或异常栈**）：

| 安全维度 | 硬性边界 | 说明 |
| --- | --- | --- |
| 支持格式 | `image/png`、`image/jpeg`、`image/webp` | 校验 MIME 与 Pillow 解码真实格式双一致，拦截伪装文件 |
| 单轮图片上限 | **`5` 张**（`MAX_VISION_IMAGES_PER_RUN = 5`） | 按 `input_file_ids` 顺序加载前 5 张有效图片；超出上限的后续图片不下载、不阻断 Run，内部标记 `vision_status="skipped_limit"` |
| 单张原图大小 | `<= 20 MiB`（`MAX_VISION_RAW_IMAGE_BYTES`） | 元数据预检 + 对象存储读上限双拦截 |
| 防解压炸弹 | 总像素 `<= 40,000,000`（`40 MP`） | 在像素解压前校验 `width * height` |
| 安全缩放 | 长边 `<= 2048px`（`MAX_VISION_IMAGE_EDGE`） | 超出时等比缩小（不放大），兼顾清晰度与视觉 Token 成本 |
| 归一化体积上限 | 编码前 `<= 4 MiB`（`MAX_VISION_ENCODED_IMAGE_BYTES`） | 超限时按质量阶梯（85 → 75 → 65）与温和缩边（1.0 → 0.75 → 0.5）自适应收敛；仍超限则单图降级 |
| 单图处理超时 | `15` 秒 | 独立工作线程执行，防止异常图片阻塞事件循环 |
| **隐私元数据剥离** | **强制全剥离** | 先执行 `ImageOps.exif_transpose` 校正手机拍摄朝向，再仅提取纯净像素字节重建 `Image`，彻底清除 EXIF、GPS 经纬度、相机型号、ICC Profile、XMP、PNG 文本块 |
| 不可信数据隔离 | 仅作为独立 `USER` 多模态消息发送 | 严禁写入 `SYSTEM` 角色，防止图片内文字/二维码提示词注入越权 |
| 视觉 Token 预算 | OpenAI 512px 分块公式 | `detail="low"` 固定 `85` tokens（宽高缺失或非法保守返回 `1105`）；常规 `auto`/`high` 先等比缩入 `<= 2048×2048` 框内（不放大），再将短边等比缩至 `<= 768px`（不放大），最后按 `512px` 网格计算 `85 + 170 * ceil(w/512) * ceil(h/512)`（绝不把 base64 文本送入 tiktoken）；上下文压缩时不将图片 base64 写入长期摘要 |

### 2.4 方案 A2：非识图模型温柔降级语义（前端核心约定）

Athena v2 采用 **方案 A2（温柔降级、永不粗暴阻断）**：

1. **后端不因非识图模型拒绝带图 Run**：
   - 即使用户当前选中的模型 `supports_vision = false`（或在历史带图会话中切换到纯文本模型），`POST /api/v1/sessions/{session_id}/runs` 仍正常受理并返回 `201/200`，**不会抛出 HTTP 4xx 阻断用户发送**。
   - Worker 检测到 `supports_vision = false` 时，**完全跳过对象存储图片读取与编码**（零带宽与内存浪费），将图片附件上下文中的 `vision_status` 标记为 `"unsupported_by_current_model"`，并向模型注入温和引导说明，由模型礼貌告知用户“当前模型暂不支持识图，建议切换至支持识图的模型后重试”，同时仍可继续正常处理同轮附带的 PDF / Office / 文本文件与对话上下文。
2. **识图模型单图降级同样不阻断整轮 Run**：
   - 当 `supports_vision = true` 时，每张图片在内部文件元数据上下文中被赋予以下精确状态之一：
     - `"attached"`：归一化成功并已随本轮多模态请求发送给模型；
     - `"skipped_limit"`：超过单轮 5 张上限，未加载；
     - `"image_too_large"`：原图超过 20 MiB、像素超过 40 MP 或重编码后仍超过 4 MiB；
     - `"image_invalid"`：图片损坏、解码超时或格式伪装。
3. **前端（M3.4 / M3.5）交互契约**：
   - 当当前所选模型 `supports_vision === false` 且草稿中包含图片附件时：前端展示温和非阻断提示条（例如“当前模型暂不支持识图，可直接发送或切换识图模型”），并提供快捷切换识图模型入口，**不得禁用发送按钮**。
   - 当草稿图片超过 5 张时：前端提示“单次对话最多同时识别 5 张图片，超出部分本次不会发送给识图模型”。

---

## 三、M3.3 公共 / 部门技能在线 Markdown 双形态与首页快捷应用元数据

### 3.1 双形态模型与不变式

为兼顾“零门槛在线撰写纯指南技能”与“完整 Python 脚本/多文件 ZIP 技能包”，`skills` 表及全部 Skill DTO 新增 4 个核心属性：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `format` | `"markdown" \| "package"` | `"package"`（个人技能恒为 `"markdown"`） | 技能内容形态与**双向格式锁**：`markdown` 表示在线单文件 `SKILL.md` 技能，支持在线编辑发版、**禁止通过 `POST .../versions` 上传 ZIP 包（`409 skill_package_upload_unsupported`）**；`package` 表示完整 ZIP 技能包，**禁止通过 `POST .../versions/markdown` 在线覆盖（`409 skill_markdown_edit_unsupported`）** |
| `has_scripts` | `bool` | `false` | 由当前激活版本 `manifest.scripts` 非空实时派生（列表 / 目录 / 详情均零 N+1 准确填充）；在线 Markdown 技能恒为 `false`，含 Python 脚本的已激活 ZIP 包为 `true` |
| `featured` | `bool` | `false` | 是否推荐到首页快捷应用卡片区；**个人技能强制 `featured = false`**（数据库 CheckConstraint `ck_skills_personal_format_and_featured` 兜底）；部门技能设为 `featured = true` 时仅对具备该部门读权限的用户展示 |
| `starter_prompt` | `string \| null` | `null` | 首页快捷应用卡片点击后自动填入输入框的起始引导语（去首尾空白后 `<= 500` Unicode 码点，允许 `\n`/`\r`/`\t`，禁止其余 C0 控制字符与 DEL；传空串或纯空白自动归为 `null`） |

### 3.2 `SkillSummaryResponse` 与 `SkillAdminDetailsResponse` 响应示例

适用于 `GET /api/v1/skills`、`GET /api/v1/admin/skills`、`GET /api/v1/me/skills` 及管理端详情接口：

```json
{
  "id": "skill_01j9def...",
  "slug": "md-01j9def",
  "scope": "department",
  "name": "财务报销合规审核助手",
  "description": "依据公司差旅与费用制度逐项核查报销单据合规性",
  "current_version_id": "skillver_01j9ver...",
  "current_version_label": "v1",
  "enabled": true,
  "script_requires_approval": false,
  "revision": 1,
  "updated_at": "2026-11-24T05:10:00Z",
  "dependency_check": {
    "status": "satisfied",
    "python_version": "not-checked",
    "python_implementation": "markdown-only",
    "checked_at": "2026-11-24T05:10:00Z",
    "items": []
  },
  "owner_department_id": "dept_fin",
  "visibility_grants": [
    {
      "id": "grant_01j9...",
      "target_type": "department",
      "target_id": "dept_fin",
      "include_subdepartments": true,
      "created_at": "2026-11-24T05:10:00Z"
    }
  ],
  "format": "markdown",
  "has_scripts": false,
  "featured": true,
  "starter_prompt": "请上传你的差旅或费用报销单截图/PDF，我将按公司最新报销制度逐项核对合规性。"
}
```

### 3.3 `GET /api/v1/skills` 用户可用目录排序规则

- **端点**：`GET /api/v1/skills`（`CurrentAuth`，仅返回当前用户实时可读且已启用的技能，自动过滤失效依赖与失权部门技能；纯 Markdown 无脚本技能跳过 Python 子进程探测）。
- **稳定排序规则**：
  1. `featured DESC`（推荐快捷应用置顶）
  2. `scope` 优先级：`department` > `company` > `personal`
  3. `updated_at DESC`（最近更新优先）
  4. `slug ASC`, `id ASC`（稳定兜底）
- **前端首页快捷应用消费建议**：直接从 `GET /api/v1/skills` 中筛选 `item.featured === true` 的条目渲染首页快捷应用卡片；用户点击卡片时自动选中该 `skill_id`，若 `starter_prompt` 非空则预填输入框。

### 3.4 新增管理端端点 1：解析 Markdown 文件草稿

- **端点**：`POST /api/v1/admin/skills/parse-markdown`
- **权限**：`SkillManagerAuth`（`super_admin` 与 `dept_admin` 均可调用；`member` 返回 `403 admin_required`）
- **请求格式**：`multipart/form-data`，单文件字段名 `file`（必须仅 1 个 `.md` 文件，UTF-8 编码，`<= 1 MiB`）。
- **行为**：纯解析不落库、不写对象存储、不创建版本。支持可选 YAML frontmatter（仅允许 `name` / `description` / `slug` 键；出现未知键如 `scripts` 拒绝并返回 `422 {"code": "skill_markdown_field_unsupported", "field": "<key>"}`）。
- **响应 `200 OK`（`SkillMarkdownParseResponse`）**：

```json
{
  "name": "报销审核助手",
  "description": "核对差旅和招待费用合规性",
  "guide": "# 使用指南\n请按以下 3 个步骤检查发票与行程单..."
}
```

- **错误码**：
  - `401 not_authenticated`
  - `403 admin_required` / `origin_not_allowed`
  - `413 skill_markdown_too_large`（超过 `1 MiB`）
  - `422 skill_markdown_file_invalid`（文件数量不为 1 或表单字段无效）
  - `422 skill_markdown_empty`（空文件）
  - `422 {"code": "skill_markdown_filename_invalid", "field": "file"}`（非 `.md` 后缀）
  - `422 {"code": "skill_markdown_encoding_invalid", "field": "file"}`（非合法 UTF-8）
  - `422 {"code": "skill_markdown_control_character", "field": "file"}`
  - `422 {"code": "skill_markdown_metadata_invalid", "field": "file"}`
  - `422 {"code": "skill_markdown_field_unsupported", "field": "<key>"}`
  - `422 {"code": "skill_markdown_name_invalid", "field": "name"}` / `skill_markdown_description_invalid` / `skill_markdown_slug_invalid`
  - `422 {"code": "skill_guide_invalid", "field": "guide"}` / `skill_guide_control_character`
  - `503 file_storage_unavailable`

### 3.5 新增管理端端点 2：在线创建公共 / 部门 Markdown 技能

- **端点**：`POST /api/v1/admin/skills/markdown`
- **权限**：`SkillManagerAuth` + 三层分权校验：
  - `super_admin`：可创建 `scope="company"`（须 `owner_department_id=null` 且 `visibility_grants=null`）或任意有效未归档部门的 `scope="department"` 技能；
  - `dept_admin`：仅允许创建本人管辖部门下的 `scope="department"` 技能；尝试创建 `company` 级或非管辖部门技能一律返回 `403 permission_denied`（不泄露目标部门是否存在）；
  - `member`：返回 `403 admin_required`。
- **必传请求头**：`Idempotency-Key`（`1..128` 可见 ASCII）。
- **请求体（`AdminSkillMarkdownCreateRequest`，严格 `extra="forbid"`）**：

```json
{
  "name": "财务报销合规审核助手",
  "description": "依据公司制度逐项核查报销单据合规性",
  "guide": "# 角色定位\n你是资深财务合规专员...",
  "scope": "department",
  "owner_department_id": "dept_fin",
  "visibility_grants": [
    {
      "target_type": "department",
      "target_id": "dept_fin",
      "include_subdepartments": true
    }
  ],
  "slug": "fin-expense-audit",
  "enabled": true,
  "featured": true,
  "starter_prompt": "请上传报销单截图或 PDF，我来帮你逐项核查合规性。"
}
```

| 字段 | 类型 | 必填 | 默认值 | 校验与业务规则 |
| --- | --- | --- | --- | --- |
| `name` | `string` | 是 | - | 去首尾空白后 `1..120` 字符，无非法控制字符（否则 `422 {"code": "skill_name_invalid", "field": "name"}`） |
| `description` | `string` | 是 | - | 去首尾空白后 `1..1000` 字符，无非法控制字符（否则 `422 {"code": "skill_description_invalid", "field": "description"}`） |
| `guide` | `string` | 是 | - | 非空白且 `<= 100,000` 字符，无非法控制字符（否则 `422 {"code": "skill_guide_invalid" \| "skill_guide_control_character", "field": "guide"}`） |
| `scope` | `"company" \| "department"` | 否 | `"company"` | 不允许 `"personal"`（个人技能请走 `/api/v1/me/skills`） |
| `owner_department_id` | `string \| null` | 否 | `null` | `scope="department"` 时必填且须为活跃部门；`scope="company"` 时必为 `null` |
| `visibility_grants` | `VisibilityGrantRequest[] \| null` | 否 | `null` | `scope="department"` 且省略时自动授予归属部门含子部门可读；`scope="company"` 时不允许非空 |
| `slug` | `string \| null` | 否 | `null` | 未提供或空白时按 `skill_id` 自动派生 `md-<id小写字母数字>`；显式提供时严格匹配 `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`（不合法 `422 skill_slug_invalid`，同范围碰撞 `409 skill_slug_conflict`） |
| `enabled` | `bool` | 否 | `true` | 默认立即可用；传 `false` 时创建为停用态（首版仍激活为 `current_version_id=v1` 以便后台展示名称与版本） |
| `featured` | `bool` | 否 | `false` | 是否推荐到首页快捷应用 |
| `starter_prompt` | `string \| null` | 否 | `null` | 去空白后空串归一为 `null`；非空须 `<= 500` Unicode 码点且无非法控制字符（否则 `422 skill_starter_prompt_invalid`） |

- **响应 `201 Created`**：`SkillAdminDetailsResponse`（`format="markdown"`, `has_scripts=false`, `current_version_label="v1"`, `revision=1`, `versions=[v1]`）。
- **幂等语义**：
  - 同一管理员以相同 `Idempotency-Key` + 相同请求体（含 `mode="admin_markdown"` 及全部字段，未传 `slug` 时以规范化 `null` 参与指纹）重试，返回 `201` 与已创建的同一 Skill（不重复创建、不产生孤儿对象）；
  - 相同 `Idempotency-Key` 但任意字段不同（或复用之前 ZIP 导入用过的 `Idempotency-Key`），返回 `409 idempotency_conflict`；
  - 数据库事务确定性失败时，自动清理本次预写入对象存储的孤儿 ZIP 包。

### 3.6 新增管理端端点 3：在线发布 Markdown 技能新版本

- **端点**：`POST /api/v1/admin/skills/{skill_id}/versions/markdown`
- **权限**：`SkillManagerAuth` + 细粒度管理权校验：
  - 目标不存在或当前部门管理员**无读权**（如其他部门未授权技能）：返回 **`404 skill_not_found`**（防存在性探测）；
  - 当前部门管理员**可读但无管理权**（如 `company` 级技能或他部门仅读授权技能）：返回 **`403 admin_required`**；
  - `member`：返回 **`403 admin_required`**。
- **双向格式防覆盖底线（硬性约束）**：
  - 若目标技能 `format !== "markdown"`（即通过 ZIP 导入的 `format="package"` 技能），调用 `POST /api/v1/admin/skills/{skill_id}/versions/markdown` **强制拒绝并返回 `409 {"detail": "skill_markdown_edit_unsupported"}`**（服务层与行锁事务内双重校验），严禁在线单文件编辑破坏 ZIP 包中的脚本、模板或参考资源；
  - 反向同理：若目标技能 `format === "markdown"`，调用 `POST /api/v1/admin/skills/{skill_id}/versions` 上传 ZIP 包同样在服务层与行锁事务内**强制拒绝并返回 `409 {"detail": "skill_package_upload_unsupported"}`**（写对象存储前即快速拦截，防止脚本或依赖混入纯文档技能且零孤儿对象）。
- **流式请求体准入保护（`UploadAdmissionMiddleware`）**：
  - `POST /api/v1/admin/skills/parse-markdown` 接入 `1 MiB + 64 KiB` 流式硬限（超限 `413 skill_markdown_too_large`）；
  - `POST /api/v1/admin/skills/markdown` 与 `POST /api/v1/admin/skills/{skill_id}/versions/markdown` 接入 `2 MiB` 流式 JSON 写体硬限（超限 `413 skill_personal_request_too_large`）。
- **必传请求头**：`Idempotency-Key`（`1..128` 可见 ASCII）。
- **请求体（`AdminSkillMarkdownVersionRequest`，严格 `extra="forbid"`）**：

```json
{
  "name": "财务报销合规审核助手（2026新版）",
  "description": "新增海外出差补贴与电子发票核验规则",
  "guide": "# 角色定位\n最新版财务报销审核指南...",
  "expected_revision": 1,
  "label": "v2.0-2026Q4",
  "activate": true
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `name` | `string` | 是 | - | 校验规则同创建（`1..120` 字符） |
| `description` | `string` | 是 | - | 校验规则同创建（`1..1000` 字符） |
| `guide` | `string` | 是 | - | 校验规则同创建（`<= 100,000` 字符） |
| `expected_revision` | `int (>=1)` | 是 | - | 修订号乐观锁；与当前数据库 `revision` 不一致返回 `409 skill_revision_conflict` |
| `label` | `string \| null` | 否 | `null` | 未提供或空白时按版本号自动派生 `v{ordinal}`；提供时去首尾空白后 `<= 64` 字符且不含任何控制字符（否则 `422 skill_version_label_invalid`） |
| `activate` | `bool` | 否 | `true` | `true`：单事务追加不可变版本并立即切换 `current_version_id` 为新版本、`revision + 1`（同步更新响应中的 `name`/`description`/`current_version_label`）；`false`：仅追加不可变版本并递增 `revision + 1`，`current_version_id` 保持原激活版本不变 |

- **响应 `201 Created`**：`SkillAdminDetailsResponse`（含最新 `revision`、当前生效版本摘要以及按版本号倒序排列的完整 `versions` 历史列表）。
- **幂等语义**：
  - 同一管理员对同一 `skill_id` 使用相同 `Idempotency-Key` + 相同请求体（含 `expected_revision`、`name`、`description`、`guide`、`label`、`activate`）重试，**优先命中幂等重放返回 `201`**，不会因为首次提交已将 `revision` 递增而误报 `409 skill_revision_conflict`；
  - 同键异参或跨目标 `skill_id` 复用键返回 `409 idempotency_conflict`。

### 3.7 升级既有端点：`PATCH /api/v1/admin/skills/{skill_id}`

- **端点**：`PATCH /api/v1/admin/skills/{skill_id}`（`SkillManagerAuth`）
- **向后兼容**：原有必传字段（`expected_revision`、`current_version_id`、`enabled`、`script_requires_approval`）及可选 `visibility_grants` 语义完全不变；不传 `featured` / `starter_prompt` 时保持库中原有值不变。
- **新增可选字段**（严格 `extra="forbid"`）：

```json
{
  "expected_revision": 2,
  "current_version_id": "skillver_01j9ver...",
  "enabled": true,
  "script_requires_approval": false,
  "featured": true,
  "starter_prompt": "请帮我检查本月部门报销清单。"
}
```

| 字段 | 传值场景 | 服务端行为 |
| --- | --- | --- |
| `featured` | 字段省略 | 保持原值不变 |
| `featured` | `true` / `false` | 在修订号 CAS 事务内更新 `featured`（若发生变化则 `revision + 1`） |
| `featured` | 显式传 `null` | **返回 `422 {"detail": "skill_featured_invalid"}`** |
| `starter_prompt` | 字段省略 | 保持原值不变 |
| `starter_prompt` | 合法非空字符串（去空白后 `<= 500` 码点） | 规范化去首尾空白后更新（若发生变化则 `revision + 1`） |
| `starter_prompt` | 显式传 `null` 或空串 / 纯空白 | **清空 `starter_prompt` 为 `null`**（若原本非空则 `revision + 1`） |
| `starter_prompt` | 超出 500 码点或含非法控制字符 | **返回 `422 {"detail": "skill_starter_prompt_invalid"}`** |

> **说明**：ZIP 包技能（`format="package"`）同样支持通过 `PATCH /api/v1/admin/skills/{skill_id}` 配置 `featured` 与 `starter_prompt` 推荐到首页快捷应用，仅禁止通过 `/versions/markdown` 在线覆盖其包内容。

---

## 四、第三期错误码速查与前端映射建议

### 4.1 技能双形态与推荐元数据错误码（M3.3）

| HTTP 状态码 | 错误码（`detail` 或 `detail.code`） | 涉及端点 | 含义与前端提示建议 |
| --- | --- | --- | --- |
| `401` | `not_authenticated` | 全部接口 | 登录态失效，跳转重新登录 |
| `403` | `admin_required` | `/admin/skills*` | 非管理员，或部门管理员试图管理 `company` 级/无管理权技能 |
| `403` | `permission_denied` | `POST /admin/skills/markdown`、`PATCH /admin/skills/{id}` | 部门管理员越权创建公司级技能或指定非管辖部门 |
| `403` | `visibility_grant_forbidden` | `POST /admin/skills/markdown`、`PATCH /admin/skills/{id}` | 部门管理员试图授权 `role`、非管辖部门，或无子树管辖权却开启 `include_subdepartments=true` |
| `403` | `origin_not_allowed` | 全部写接口 | 跨源请求被拒 |
| `404` | `skill_not_found` | `/admin/skills/{id}*` | 技能不存在或对当前部门管理员不可读（防存在性探测） |
| `404` | `skill_version_not_found` | `PATCH /admin/skills/{id}` | 指定的 `current_version_id` 不属于该技能 |
| `409` | `skill_markdown_edit_unsupported` | `POST /admin/skills/{id}/versions/markdown` | 目标为 ZIP 技能包（`format="package"`），请改用「上传新版本 ZIP」入口 |
| `409` | `skill_package_upload_unsupported` | `POST /admin/skills/{id}/versions` | 目标为在线 Markdown 技能（`format="markdown"`），请改用「在线编辑 Markdown」发版入口 |
| `409` | `skill_slug_conflict` | `POST /admin/skills/markdown` | 同一公司或同一部门下已存在相同 `slug`，请更换标识 |
| `409` | `skill_revision_conflict` | `POST .../versions/markdown`、`PATCH /admin/skills/{id}` | 技能已被他人更新，请刷新最新详情后重试 |
| `409` | `idempotency_conflict` | `POST /admin/skills/markdown`、`POST .../versions/markdown` | 重复使用了不同的请求体提交同一 `Idempotency-Key`，请生成新键重试 |
| `413` | `skill_markdown_too_large` | `POST /admin/skills/parse-markdown` | 上传的 `.md` 文件超过 `1 MiB` 上限（含流式中间件 `1 MiB + 64 KiB` 熔断） |
| `413` | `skill_personal_request_too_large` | `POST /admin/skills/markdown`、`POST .../versions/markdown` | JSON 请求体超过流式中间件 `2 MiB` 保护上限 |
| `422` | `skill_featured_invalid` | `PATCH /admin/skills/{id}` | 显式传入了 `featured: null`，须为布尔值 `true` 或 `false` |
| `422` | `skill_starter_prompt_invalid` | `POST /admin/skills/markdown`、`PATCH /admin/skills/{id}` | 引导语超过 500 字符或包含非法控制字符 |
| `422` | `skill_slug_invalid` | `POST /admin/skills/markdown` | `slug` 格式不合法（仅允许小写字母、数字和连字符，首尾须为字母数字，1..64 字符） |
| `422` | `skill_version_label_invalid` | `POST .../versions/markdown` | 版本标签超过 64 字符或包含控制字符 |
| `422` | `skill_name_invalid` / `skill_description_invalid` / `skill_guide_invalid` / `skill_guide_control_character` | `POST /admin/skills/markdown`、`POST .../versions/markdown` | 名称、简介或指南正文校验失败（返回 `{"code", "field"}` 便于表单精准标红） |
| `422` | `skill_markdown_file_invalid` / `skill_markdown_empty` / `skill_markdown_filename_invalid` / `skill_markdown_encoding_invalid` / `skill_markdown_metadata_invalid` / `skill_markdown_field_unsupported` | `POST /admin/skills/parse-markdown` | `.md` 文件解析校验失败 |
| `422` | `owner_department_required` / `owner_department_not_allowed` / `owner_department_not_found` / `visibility_grants_not_allowed` / `visibility_grant_invalid` / `visibility_grant_target_invalid` / `visibility_grant_duplicate` / `visibility_grant_role_invalid` / `visibility_grant_target_type_invalid` / `visibility_grant_department_not_found` | `POST /admin/skills/markdown`、`PATCH /admin/skills/{id}` | 三层可见性与归属部门授权配置不合法（注：复数 `visibility_grants_invalid` 仅用于 multipart 表单端点 `POST /api/v1/admin/skills/import` 解析 JSON 表单字段失败） |
| `422` | `idempotency_key_invalid` | `POST /admin/skills/markdown`、`POST .../versions/markdown` | `Idempotency-Key` 含非可见 ASCII 或长度超限 |
| `503` | `file_storage_unavailable` | 文件上传/解析与技能包写入端点 | 对象存储暂时不可用，请稍后重试 |

### 4.2 会话附件预览与文本提取错误码汇总（M3.1）

| HTTP 状态码 | `detail` | 涉及端点 | 含义 |
| --- | --- | --- | --- |
| `404` | `session_file_not_found` | `GET .../preview`、`GET .../content` | 会话或文件不存在、或非本人会话 |
| `415` | `file_preview_unsupported` | `GET .../preview` | 非 PNG / JPEG / WEBP 安全位图，禁止内联渲染（前端应回退为下载或文本抽屉查看） |
| `415` | `file_not_text` | `GET .../content` | 非纯文本且非可提取办公文档 |
| `422` | `document_no_extractable_text` | `GET .../content`（及 `extraction_error_code`） | 扫描版 PDF 或空白文档无文本层 |
| `422` | `document_encrypted` / `document_macro_unsupported` / `document_extract_too_large` / `document_page_limit_exceeded` / `document_archive_limit_exceeded` / `document_extracted_text_too_large` / `document_extract_timeout` / `document_type_unsupported` / `document_parse_failed` | `GET .../content`（及 `extraction_error_code`） | 文档解析受安全策略拦截或解析失败，前端展示可读原因并保留原件下载入口 |
| `422` | `file_offset_invalid` / `file_range_too_small` / `file_content_not_text`；Query 越界返回 FastAPI 字段错误数组 `[{"loc": ...}]` | `GET .../content` | 分页偏移或 UTF-8 解码边界错误（注：`session_file_range_invalid` 仅由服务层或内置工具 `session_file_read` 直接校验抛出） |
