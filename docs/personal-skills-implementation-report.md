# 个人 Skill 实施与验收报告

日期：2026-11-19

设计基线：[personal-skills-design.md](personal-skills-design.md)。用户已明确授权实施。Root 委派后端 Implementer 和 frontend-implementer 分别实施；经一轮修复，前后端独立 Reviewer 均为 PASS。2026-11-19 已在本地 Docker 测试环境完成真实 PostgreSQL、MinIO、真实模型和双用户浏览器闭环；未部署到目标环境。

## 已实现行为

- 新增“我的技能”页面，支持在线编写、导入单个 `.md`、编辑新版本、启停和删除。
- 文件控件 `accept=".md"` 且不可多选；前后端均校验后缀与大小，后端进一步验证 UTF-8、正文及元数据。文件最大 1 MiB，名称、描述、正文上限为 120、1000、100000 字符。
- 导入只解析并填入表单，点击保存才发布并启用。元数据只支持 name、description、slug，拒绝 scripts、dependencies 及其他未知字段；正文代码块可以保留，不注册或执行脚本。
- 个人 Skill 仅本人可管理和使用。用户可用目录包含公共项及本人启用项；公共管理接口不暴露个人项。
- 版本保存以 revision 校验和行锁原子发布，支持请求幂等；数据库提交响应丢失或取消时保留可能已被引用的包对象。
- 聊天目录及 Run 快照显示公共/个人来源，个人项显示“纯文档”；模型自动选用和用户手动选择均使用现有 skill_load 链路。个人名称、描述、正文进入 USER 消息，公共 Skill 保持受信上下文，SYSTEM 对个人内容仅提供固定边界说明。
- 提交、重试按可信 owner_id 构造快照，子 Run 只继承父快照授权子集；删除与停用不破坏既有 Run 的固定版本，旧快照默认 public。
- 个人脚本执行请求在服务端拒绝，不增加工具权限，不绕过审批。
- 解析前复用请求准入中间件，对个人 Markdown multipart 设置 1 MiB 文件加 64 KiB 开销的原始请求上限，对个人创建和发布 JSON 设置 2 MiB 上限；流式计数不依赖 Content-Length。文件自身仍严格限制 1 MiB。
- 旧幂等请求返回当前一致详情，原操作版本仅用于内部幂等身份和对象清理。个人数据库连接错误与文件对象存储错误分别返回相应的 503 错误码。

## 主要代码位置

| 范围 | 位置 |
| --- | --- |
| 归属和版本 | `backend/src/domain/skill/`、`backend/src/adapter/skill/` |
| Markdown 解析与发布 | `backend/src/app/skill/personal.py`、`service.py` |
| HTTP 契约 | `backend/src/api/skill.py`、`dto/skill.py`、`converter/skill.py`、`backend/src/main.py` |
| Run 隔离与来源 | `backend/src/adapter/run/repository/lifecycle.py`、`backend/src/app/run/context.py`、Run DTO/converter |
| 迁移 | `backend/migrations/versions/20261119_0031_add_personal_skills.py` |
| 页面与聊天 | `frontend/src/features/skills/PersonalSkillsPage.tsx`、`PersonalSkillEditorDialog.tsx`、`SkillCatalogPicker.tsx`，路由/侧栏与 Run 来源展示 |

## 验证证据

| 验证 | 结果及边界 |
| --- | --- |
| 后端实施前基线 | 1059 passed、1 skipped、68 deselected |
| 后端实施后非集成回归 | 第一轮修复后 Implementer：1097 passed、3 skipped、67 deselected |
| Root 聚焦复核 | 第一轮修复后个人 API、解析、Repository、上下文、提交恢复、现有快照恢复和请求准入：44 passed |
| 后端全部源码静态检查 | 第一轮修复后 Root：Ruff 通过；strict mypy 348 个源码文件通过 |
| 前端回归 | 第一轮修复后 Root 全量复核：35 个文件、310 项测试通过 |
| 前端类型与构建 | frontend-implementer：typecheck/build 通过；存在构建 chunk 大小提示 |
| 个人浏览器流程 | frontend-implementer：2 个 Playwright mock E2E 通过，覆盖管理流程、390px 导入及聊天来源 |
| 管理员浏览器回归 | frontend-implementer：8 个现有 mock E2E 通过 |
| 布局与输入保护 | Root 检查桌面 1280×900 列表与 390×844 编辑器截图，无横向溢出或主要按钮遮挡；延迟解析浏览器复验确认等待期间字段禁用，原输入覆盖路径已消除 |
| 迁移静态检查 | Root：单一 head `20261119_0031`，增量 PostgreSQL offline SQL 可生成 |
| 真实 PostgreSQL | 首轮实施时未通过环境连通性检查；后续已在本地 Docker 测试库完成复验，见下节 |
| 真实 MinIO / 模型 | 首轮实施时未配置；后续已在本地 MinIO 和真实模型链路完成复验，见下节 |

### 2026-11-19 本地 Docker 端到端复验

- Docker 栈使用当前工作区代码运行；PostgreSQL 迁移位于 `20261119_0031`，`skills` 已包含 `scope`、`owner_id`、`deleted_at`。
- 真实 PostgreSQL 集成测试通过：`1 passed`；个人 Skill 后端聚焦测试：`35 passed`；前端个人 Skill 与错误文案：`11 passed`；既有 Skills PostgreSQL/MinIO 回归：`2 passed`。
- 真实 API、PostgreSQL、MinIO 和模型探针共 `119` 项通过，覆盖按需加载、手动选择、公共/个人同名、版本固定、停用删除、历史 Run 保留、双用户隔离及并发保存/删除收敛。
- 系统 Chrome 双用户浏览器流程通过 14 个步骤，覆盖登录与导航、在线创建、非法扩展名拒绝、大写 `.MD` 与 frontmatter 导入、编辑 v2、启停、删除、390px 布局、公共/个人来源同屏展示、B 越权读取返回 404，以及相同文档跨用户导入。
- 模型链路存在一次瞬时 `llm_empty_output`，有界重试后成功。真实模型未自然触发个人 Skill 的 `skill_execute`；该拒绝边界由服务/API 测试覆盖。
- `scripts/test-docker.sh up` 当前先执行迁移、后构建业务镜像；首次启动可能在旧镜像上迁移，需要再次执行 `up` 才能应用新 head。该顺序问题未在本次功能提交内修改。

新增 `backend/tests/integration/test_personal_skills_postgres.py` 在随机 schema 中覆盖旧公共记录回填、同内容跨用户、slug 范围唯一性、归属约束及软删除复用。需把配置安全载入进程环境后运行；未配置时跳过，连接拒绝时明确标记环境跳过。

```bash
.venv/bin/python -m pytest -q -m integration backend/tests/integration/test_personal_skills_postgres.py
```

浏览器 mock 测试入口：

```bash
cd frontend
npx playwright test e2e/personalSkills.spec.ts
```

## 上线与工作树边界

- 本地 Docker 测试库已执行新增迁移；目标环境仍需执行迁移，本次未部署上线。
- 本地测试已使用专用文件 MinIO 配置完成真实对象存储复验；目标环境仍需配置已有 `ATHENA_FILE_MINIO_*`。
- 原有前端未提交修改已保留，Root 通过基线 patch 反向检查确认仍完整。本次范围的 diff 空白检查通过；原有 `frontend/e2e/admin.spec.ts` 文件末尾空行提示不属于本次修改。
- 本地 PostgreSQL、MinIO、真实模型和双用户浏览器主链路已验证；不得据此报告已部署上线。

## 独立审查与最终结论

初始 Reviewer 模型被限流，未产生结论。按用户要求，全局 Reviewer 配置已切换为 `opencode-go/deepseek-v4.1-flash`、推理强度 `max`，并重启前后端独立审查。

第一轮审查均为 CHANGES_REQUIRED，Root 核验后交回原 Implementer 修复：

- 前端：解析期间表单输入被响应覆盖；多行描述误拒；重开编辑器残留旧文件名。另采纳移除内部目录修订号的非阻断建议。
- 后端：个人内容进入 SYSTEM 通道；个人请求体缺少解析前大小准入；旧幂等请求返回当前指针与旧正文的混合状态；数据库传输异常投影遗漏新路由。
- 幂等契约明确为“不重复写入，返回该技能当前一致详情”，不增加额外状态存储。

第 1 轮修复后，前端独立重审 PASS，Reviewer 另外运行个人技能及错误文案测试 11 项通过；后端独立重审 PASS，确认原四项问题均已解决，未发现新增阻断问题。没有进入第二轮修复。

Root 验收结论：已确认的个人 Skill 功能及权限边界已实现，必要的可运行测试、源码检查和本地 Docker 端到端复验通过，无未处理代码阻断项。前端全量 310 项、后端非集成全量 1097 项、Root 后端聚焦 44 项及后续真实链路复验结果见上表与本节。本结论不表示目标环境已迁移或部署上线。
