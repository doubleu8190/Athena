# 知识库 MinIO 实施文档

日期：2026-11-09。状态：代码、单元测试、静态检查、独立 Review 和隔离环境真实
MinIO/PostgreSQL 联调已完成；实际数据清理/切换和生产部署尚未执行。本文不表示
环境已切换。

依据：[知识库 MinIO 设计](knowledge-minio-design.md)。已确认的产品范围以该设计第 1 节为准。本文把范围拆成可实施、可验证的任务；新增端口形状、配置默认值及异常名称属于实施建议，随本文整体确认。

## 1. 交付目标与约束

将原件唯一生产存储切换为部署级 MinIO 私有桶，API 代理上传下载，知识库 Worker 直接读取。保留 PostgreSQL 检索、上传配额/预留/幂等、任务租约与 fencing、现有文件限制及权限模型。手动文本继续存数据库，本地仅留暂存和模型/OCR 缓存。

清空测试文档和处理数据，保留知识库、项目关联及其他领域数据；不迁移旧文件。不新增数据库表、前端管理页、客户端直传、大文件流式 API、通用文件服务或本地存储选项。预计无需 Alembic 结构迁移。

仅在 Athena_v2 修改。实施前重查分支、工作树与 AGENTS.md；当前 v2 有大量其他未提交改动，不得重置、覆盖或混入提交。下列文件路径均相对仓库根目录，新增文件可按现有导出方式微调，职责不能随意改变。

## 2. 已核对的现状及差距

| 代码 | 现状 | 必要变化 |
| --- | --- | --- |
| `backend/src/domain/knowledge/ports.py` | 已有 OriginalStore 四个方法，包含以引用快照清理的 sweep_orphans | 复用 put/get/delete；将候选列举与业务引用判断分开，防止存储实现持有仓储或依赖过期快照。 |
| `backend/src/infrastructure/knowledge/storage.py` | 本地 key 校验、哈希校验、不可变写入、文件 mtime 清理 | 由 MinIO 适配器替代，保持键和内容约束。 |
| `backend/src/app/knowledge/service.py` | 预留 → put → DB 提交；source 先鉴权；删除还遍历返回键 | 保留上传/读取顺序，删除不直接调用对象存储。 |
| `backend/src/adapter/knowledge/repository.py` | 文档删除已登记 cleanup 并返回空列表；引用快照含来源/预留，单键检查仅含来源 | 复用删除任务；补齐单键检查的上传预留引用。 |
| `backend/src/app/knowledge/worker.py` | 已有 cleanup、预留回收、每 100 次 run_once 扫描、解析重试 | 保留调度，按存储异常分类；孤儿按候选逐项复核引用。 |
| `backend/src/main.py` | 上传准入同时检查暂存目录与本地原件目录空间 | 移除原件路径参数和磁盘检查，保留暂存空间、请求体限制和上传槽位。 |
| `backend/src/bootstrap/tool.py` | API/Run Worker 共享查询服务组装，类型指向本地实现 | 改为端口类型，统一配置和资源生命周期。 |
| `backend/src/knowledge_worker.py` | 独立组装本地存储和模型 | 与 API/Run Worker 使用相同 MinIO 配置构建。 |

## 3. 阶段一：配置、SDK 与存储契约

### 3.1 SDK 兼容验证

实施起点先核对 MinIO 官方 Python SDK 文档、目标服务版本和 Python 版本，确认对象上传、stat、get、删除、分页列举、HTTP 连接池及超时配置。固定兼容 SDK 依赖于 `pyproject.toml` 的 knowledge extra；关闭知识库时不得因缺少可选 SDK 而导入失败。

SDK 调用必须离开事件循环，有界并发，并显式设置有限连接/读取超时和有限重试。建议连接超时 5 秒、读取超时 30 秒、最多 2 次 SDK 重试、每进程最多 4 个并发存储操作；这些为首期默认值，不新增一组调参页面。SDK 机制若不支持等价行为，先记录差异再调整方案。

协程取消不代表底层线程已停止：并发额度直到实际同步调用结束后才释放；关闭时先停止新操作，再收尾执行中的调用和连接资源。put 取消或超时均视为结果可能已成功，不能立即删对象或释放有效预留。

输出：选定依赖版本、兼容依据、资源和取消处理方案；随后进入代码实施。

实施记录：SDK 固定为 `minio==7.2.20`，其 metadata 要求 Python >=3.9，已在
Python 3.14 可编辑环境中安装；以伪客户端验证超时配置、连接释放、取消后容量
占用和关闭等待，并使用本地隔离 MinIO 服务及隔离私有桶验证真实 SDK/适配器
兼容性，包括启动桶检查、内容寻址 put/get、幂等 delete、对象列举时间字段和
相同 key 并发写入。验证记录不包含 endpoint、凭据或其他 secret。

### 3.2 配置与组装

建议新增 `backend/src/adapter/knowledge/storage.py`，容纳类型化配置和 `MinioOriginalStore(OriginalStore)`，集中环境变量解析入口供各进程复用，不在每个入口重复解析。

必填：`ATHENA_KNOWLEDGE_MINIO_ENDPOINT`（host[:port]）、`ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY`、`ATHENA_KNOWLEDGE_MINIO_SECRET_KEY`、`ATHENA_KNOWLEDGE_MINIO_BUCKET`。TLS：`ATHENA_KNOWLEDGE_MINIO_SECURE=true`，严格布尔解析。移除 `ATHENA_KNOWLEDGE_STORAGE_ROOT`。不将凭据写入日志、返回值或文档示例。

桶由部署方预建，专属于该环境知识库，禁止匿名访问；首期不启用版本控制和保留锁。运行凭据具备读写删、列举和必要桶检查权限，应用不创建桶或修改策略。启动检查不能仅以“桶存在”声称所有数据权限已验证，读写删权限通过联调冒烟确认。

知识库关闭时不构造客户端、不校验 MinIO 配置。启用时严格校验并检查桶；失败给出安全错误码，不回退本地。初次启动服务不可达时退出并报告原因，运行期间故障通过请求错误和后台重试处理。

### 3.3 存储端口与异常

保留 `put(key, bytes)`、`get(key) -> bytes`、幂等 `delete(key)`。将 `sweep_orphans(referenced_keys)` 替换为 `iter_orphan_candidates(*, older_than_seconds=86400)` 异步迭代候选 key。适配器按服务端对象修改时间及合法键筛选，分页读取，不持有数据库引用集合、不自行决定删除。Worker 负责单键引用查询及删除。生产不再保留旧 sweep 接口；同步更新假存储与相关测试。

沿用服务端生成的 `<owner哈希>/<source_id>/<hash前两位>/<sha256>` 键。put 验证字节哈希与键一致，已有同内容对象可重放，冲突拒绝；不把 ETag 当作 SHA-256，也不只信对象元数据。当前业务不会为同键生成不同合法内容，具体并发写入策略须通过真实测试证明，不声称普通上传天然具有“仅创建”语义。

get 有界读取，至少沿用当前文件最大尺寸，并校验实际 SHA-256 与键一致；响应连接在成功、失败和取消路径均释放。delete 将对象已不存在视为成功，不能将网络错误吞成不存在。

在 `backend/src/domain/knowledge/model.py` 定义稳定存储异常并从 `domain/knowledge/__init__.py` 导出，建议区分暂不可用、未找到、拒绝访问、内容损坏/冲突；异常只携带安全信息，adapter 内部转换 SDK 异常。共用日期处理使用 `common/date.py`，不新增无类型 JSON 元数据持久化。

阶段出口：配置测试、存储适配器单元测试通过；真实 MinIO put/get/delete/候选列举通过；并发与取消不会突破操作数量限制。

## 4. 阶段二：接入业务与一致性

### 4.1 上传、下载与准入

修改 `app/knowledge/service.py`、`api/knowledge.py`：

- 保留预留、写原件、数据库提交顺序；存储失败不得登记 ingestion。相同幂等键重试复用现有预留逻辑，DB 提交不明确时不补偿删除。
- 下载仍先查询授权来源；原件缺失返回安全的 source_not_found（404），暂不可用返回 503，桶权限/完整性错误返回安全 503 并记录区别错误码供运维定位。未授权请求不调用 MinIO。
- create/replace/source 的存储异常映射一致，保留其他已有 400/409/413/429 语义和下载响应头，不将所有异常笼统映射成 404。
- 删除仅依赖数据库软删除与 cleanup 登记，不调用存储；保持外部返回值和仓储现有返回类型，避免无关端口重构。

修改 `main.py` 上传中间件：移除 `storage_path` 与原件目录磁盘检查，保留 `ATHENA_KNOWLEDGE_UPLOAD_MIN_FREE_BYTES` 对暂存盘的检查、multipart 前请求体限制、全局上传槽位及其释放。不能因取消本地原件而取消所有磁盘保护。

### 4.2 Worker 与孤儿回收

修改 `app/knowledge/worker.py` 和必要仓储查询：

- ingestion：暂时故障进入现有有限重试；缺失、内容冲突/损坏、权限配置错误记录明确失败码，不无限重试。保留手动重试入口和原发布 fencing。
- cleanup：网络/权限故障保留任务并退避自动重试，修复后继续；某个对象已删或部分完成可重放。租约失效不得完成数据库清理。
- 过期预留：沿用 claim → can_delete（token/fencing）→ delete → finish；适配存储异常，单项失败保留预留与配额，不中止本批后续对象。
- 孤儿：遍历至少 24 小时前的合法候选，逐项查询来源及上传预留引用，确认均不存在才删除。修改 `is_storage_key_referenced` 包含两类引用，不能仅查来源表。数据库查询/列举失败则停止相关删除，不使用空集合替代失败。
- 竞争依据：合法对象键为新来源 ID，新的上传不会重新引用无来源且无预留的旧 key；迟到上传的 DB 提交仍受预留 fencing 保护。通过暂停点测试覆盖扫描、预留回收、迟到 put/提交及多 Worker 交错；如果发现该不变量不成立，先修订设计，不用一次存在性查询冒充原子保证。
- 未完成分段上传与已完成孤儿对象分别处理：按实际 SDK 行为核对桶生命周期的分段回收规则，写入部署说明；应用不擅自更改桶生命周期。
- 清理失败须可观察，记录任务/操作/安全错误码，不输出原文或凭据。

阶段出口：存储故障不误删有效原件，不使已删除文档恢复可见；解析和 cleanup 重试符合各自规则。

## 5. 阶段三：进程接线与移除本地实现

- `backend/src/bootstrap/tool.py`：共享查询服务组装；bundle 的原件字段改为 OriginalStore 端口，组装新实现，并加入 start/aclose 生命周期。适配 API 和 Run Worker 共用路径。
- `main.py`：移除本地实现导入及类型注解，确保启用/关闭和启动失败时的资源收尾。
- `knowledge_worker.py`：改为同一配置构建；存储、模型、数据库部分初始化失败时也要收尾，不只覆盖消费者运行后的 finally。
- `infrastructure/knowledge/__init__.py`：移除 LocalOriginalStore 导出；删除旧 storage.py。不保留生产本地后端。
- 全仓搜索 `LocalOriginalStore`、`ATHENA_KNOWLEDGE_STORAGE_ROOT` 和原件目录检查，清除运行代码残留；旧说明中的历史叙述可保留但须明确已替代。

阶段出口：知识库关闭时无 SDK/凭据仍可启动；启用时 API、知识库 Worker、Run Worker 使用同一配置；进程关闭无泄漏。

## 6. 阶段四：测试数据清理与部署材料

原方案建议新增 `backend/scripts/reset_knowledge_documents.py`，提供预览及显式清理模式；该脚本及其专属测试已撤销，当前不提供知识库测试数据清理脚本。以下清理步骤保留为人工操作设计，不代表现有可执行工具。

执行步骤：

1. 暂停相关 API 写入、知识库 Worker 与可能使用知识库的 Run Worker，等待在途工作收尾。记录保留的知识库和项目关联标识。
2. 在同一数据库事务中按实际外键顺序清除 `knowledge_jobs`、`knowledge_requests`、`knowledge_upload_reservations`、`knowledge_chunks`、`knowledge_document_versions`、`knowledge_documents`、`knowledge_sources`；清空 `knowledge_upload_slots` 的 token/lease_until，保留槽位行。禁止无边界 CASCADE。
3. 校验文档相关数据归零，`knowledge_bases`、`project_knowledge_bases` 和其他领域数据保持原状后提交；失败回滚。脚本应可在空数据上重复执行。
4. 单独核对旧测试原件路径后清理，排除 OCR/模型缓存。不迁移旧文件；清理脚本与文件删除均须记录实际执行结果。
5. 部署预建私有桶和凭据，更新配置，启动新版进程，执行端到端冒烟。

不支持在已写入新文档后直接退回旧本地存储版本。若切换失败，暂停写入并修复 MinIO/配置；不能把旧目录当作新数据的回退副本。

更新 `.env.example`、README 的依赖/私有桶/权限/分段回收/启动/重试说明，以及 `knowledge-base-design.md`、`knowledge-base-implementation-plan.md` 的本地存储约定，链接本方案。不得提交真实凭据。

## 7. 测试矩阵与验收证据

| 层次 | 测试落点 | 必测行为 |
| --- | --- | --- |
| 存储 | 改造 `backend/tests/test_knowledge_storage.py`；新增 MinIO 集成测试 | 键/哈希验证、幂等 put、冲突、缺失对象、连接释放、分页、超时、取消后的资源上限。 |
| 应用与 API | `test_knowledge_upload.py`、`test_knowledge_service.py`、`test_knowledge_api.py` | create/replace、上传失败不入队、未知提交结果不删对象、授权前不读存储、错误映射、删除不访问存储。 |
| Worker | `test_knowledge_worker.py`、`test_knowledge_worker_recovery.py`、`test_knowledge_orphan_sweep.py` | ingestion 与 cleanup 不同重试策略、预留失败恢复、逐键引用复核、失租、部分完成及并发清理。 |
| 入口 | `test_knowledge_upload_limits.py`、`test_knowledge_optional_import.py` 及新增组装测试 | 暂存盘检查保留、槽位释放、无本地原件路径、可选依赖、各入口配置和关闭。 |
| DB + MinIO | `integration/test_knowledge_postgres.py`、`integration/test_knowledge_jobs_postgres.py` 及新增集成文件 | 真实预留/配额/幂等竞争、DB 提交失败、对象实际成功但响应丢失、来源/预留引用竞争、cleanup 恢复。 |
| 数据切换 | 新增隔离 PostgreSQL 清理脚本测试 | 保留配置、清理顺序、事务回滚、重复执行、槽位复用、不影响其他领域。 |
| 回归 | 现有 search/parser/embedding 测试和端到端冒烟 | 上传 → Worker 解析 → 检索 → 版本下载 → 删除 → 原件回收；中文 OCR 行为不回退。 |

单元测试使用仅测试目录内的内存假存储，移除对本地 rglob/mtime 的生产实现断言。真实 MinIO 集成测试使用隔离私有桶、真实 PostgreSQL；禁止测试清理指向业务桶。原有 PostgreSQL 测试需适配内存假存储或真实桶，不能因移除本地后端而删除既有并发/配额场景。

测试命令沿用 README：`.venv/bin/python -m pytest`、`.venv/bin/python -m ruff check .`、`.venv/bin/python -m ruff format --check .`、`.venv/bin/python -m mypy`；集成测试使用 `.venv/bin/python -m pytest -m integration backend/tests/integration`，先满足依赖和隔离环境。根据工作树已有失败记录本次与原有问题，不能用排除新增失败的方式报告通过。

交付证据需分别列出单元/静态检查结果、真实 MinIO+PostgreSQL 结果、脚本演练与实际切换结果。没有环境时明确未验证，不将 mock 通过当成真实存储验收；实测故障注入须说明注入点，不能只声明覆盖。

本次真实证据（2026-11-09）：使用隔离私有桶和隔离 PostgreSQL schema，未记录
endpoint、凭据或任何 secret。

- 真实 MinIO 适配器测试：3 passed。
- PostgreSQL + MinIO 联合一致性测试：3 passed。
- 知识库测试：174 passed / 2 skipped。
- 全仓测试：643 passed / 6 skipped。

跳过项为可选真实模型或外部服务测试，不是失败。以上结果仅证明代码、适配器和
隔离环境联调，不等于实际数据已清理或切换，也不等于生产部署已经完成。

## 8. 执行与完成标准

整体方案确认后，Root 委派 Implementer 编码、测试及修复；完成后启动 Terra Reviewer 独立审查实际 diff。有效问题交回原 Implementer，再次 Review，最多三轮。Reviewer 只审查，最终 Root 验收。

实施阶段完成须满足：目标行为已实现、必要测试通过、独立 Review PASS、无阻断问题、配置及切换说明完整。环境切换单独报告，不能将代码完成等同数据已清理或服务已部署。

当前无需新增产品决策。代码、独立 Review 和隔离环境真实 MinIO/PostgreSQL 联调
已完成；实际数据清理/切换、生产环境部署与生产端到端冒烟尚未执行。切换完成前
不得把隔离环境测试结果表述为数据已切换或生产部署完成。
