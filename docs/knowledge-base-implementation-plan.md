# 知识库详细实施方案

状态：待用户确认，未授权代码实施。本文承接 knowledge-base-design.md 中已确认需求；
对早期草案的现状、解耦与实施细节作出收敛。技术默认值随本方案一并待确认。

## 1. 目标、范围与当前基础

本次交付 Athena 内的知识库后端：用户可管理知识库，上传资料或提交文本，查看
处理状态，替换和删除文档，测试混合检索，查看片段和下载原件，并关联本人项目。
提供前端可调用的 API，不建设独立产品或独立部署系统；当前没有前端工程，本轮
不把页面实现列入交付。对话自动检索、模型答案引用消息协议另行接入。

已确认：PostgreSQL + pgvector、本地磁盘、应用层中文分词、关键词与语义混合
检索、本地 embedding 参考 Athena、OCR、DOCX/XLSX/PPTX；不支持 DOC/XLS/PPT。
知识库归用户，允许同用户多项目复用。后台独立 worker 使用 PostgreSQL 持久化任务。

当前实查已有认证、项目、Session、消息、MCP、记忆、本地模型适配器，以及 LLM
相关代码。pyproject.toml 已包含 pgvector、sentence-transformers、transformers。
现有 adapter/memory/model.py 已使用 vector(1024)、TSVECTOR 和 GIN；迁移目录为
backend/migrations/versions，目前可见最新编号 0008。实施时重新读取迁移 head，
禁止以本文编号覆盖其他并行工作的迁移。

尚未发现知识库、文件上传、Office/OCR 解析或持久化文档处理任务实现。
旧草案中的“无模型调用”不能作为当前事实。本文不修改其他正在进行的 LLM 工作。

## 2. 代码边界与修改位置

| 位置（backend/src 下） | 责任 |
| --- | --- |
| domain/knowledge/model/ | 实体、状态、来源定位、请求结果、异常 |
| domain/knowledge/ports.py | 仓储、原件存储、解析、OCR、embedding、分词及任务处理需要的端口 |
| app/knowledge/ | 目录管理、导入、版本管理、检索及任务编排 |
| adapter/knowledge/{model,converter,repository}/ | ORM、纯转换、数据库查询与事务 |
| infrastructure/knowledge/ | 磁盘、解析器、分词、OCR、embedding 适配器 |
| api/knowledge.py、api/dto/knowledge.py、api/converter/knowledge.py | 协议、DTO、异常映射 |
| app/project/knowledge.py、adapter/project/ 下相应文件 | 项目关联及范围解析，避免核心依赖项目 |
| knowledge_worker.py | 后台入口，复用应用配置和数据库组装，不另建部署体系 |

修改已有 main.py 挂载路由与初始化资源；修改 adapter/models.py 聚合 ORM；增加
Alembic 迁移、依赖、配置示例与开发文档。新增目录只按实际切片创建。

依赖方向：HTTP/项目集成 → 知识应用服务 → 知识领域端口；适配器实现端口。
核心不导入认证、项目、Session、消息、MCP 或记忆模块。身份由 HTTP 的 CurrentAuth
注入，不接受客户端 owner。数据库外键与同库原子事务不因代码解耦被禁止。

已有 LocalEmbedding 位于 infrastructure/memory/models.py，不能让知识库直接依赖
记忆模块。优先把通用 embedding 实现及必要的有界推理机制迁到中立的
infrastructure/embedding/，保留旧导入导出以减少调用方改动；记忆和知识库都通过
各自端口使用。不得顺带迁移 reranker 或重写记忆逻辑。此项需跑记忆回归测试。
同一进程同一模型 profile 可复用实例，由组装层负责；worker 进程单独加载。

## 3. 技术默认方案与验证门槛

| 能力 | 建议默认 | 实施前验证 |
| --- | --- | --- |
| 中文分词 | jieba 搜索模式，英文标识符补充提取 | 中文词句、错误码、型号、含标点代码的入库查询一致性 |
| 全文检索 | 分词结果生成 simple TSVECTOR，GIN，ts_rank_cd | 全文匹配与排序真实 PostgreSQL 测试 |
| 语义检索 | BAAI/bge-m3，1024 维，余弦距离 | 固定模型 revision、有限数值及非零向量、真实样例 |
| 混合排序 | RRF，常数 60，两路等权 | 两路分数不直接相加，排序确定性 |
| OCR | 本地 PaddleOCR，中英文 | Python/系统/推理框架兼容性，扫描中文 PDF 和图片实测 |
| PDF | pypdfium2 提取文本及渲染 | 页码、混合 PDF、加密损坏文件、资源释放 |
| Office | python-docx、openpyxl、python-pptx | 段落表格、工作表单元格、幻灯片与嵌入图片 |
| 图片 | PNG、JPEG，Pillow 解码与像素限制 | 损坏图、超大像素图、方向处理 |

依据：[jieba](https://github.com/fxsjy/jieba)、
[PostgreSQL 排序](https://www.postgresql.org/docs/current/textsearch-controls.html)、
[pgvector](https://github.com/pgvector/pgvector)、
[PaddleOCR 安装](https://www.paddleocr.ai/latest/en/version3.x/installation.html)、
[pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html)、
[openpyxl](https://openpyxl.readthedocs.io/en/stable/tutorial.html)。

以上是建议选型，不声称已在本机安装或验证。K0 阶段固定实际可用版本和模型
revision；若无法满足范围或需更换方案，先报告并确认，不静默删除 OCR 或 Office。
服务运行时只读本地模型，下载/准备属于部署步骤，文档内容不发送外部服务。
CPU 为可用基线，MPS/GPU 按实际兼容性启用，不假设所有模型都支持同一设备。

## 4. 数据模型与约束

所有实体使用业务前缀 UUID、UTC 时间。内部 owner 外键指向 users；HTTP 不输出
owner、物理路径或 worker 内部凭据。表及关键字段如下：

| 表 | 字段 |
| --- | --- |
| knowledge_bases | id、owner_id、name、description、created_at、updated_at、deleted_time |
| knowledge_sources | id、owner_id、storage_key、filename、media_type、size_bytes、sha256、created_at |
| knowledge_documents | id、owner_id、knowledge_base_id、title、active_version_id、pending_version_id、created_at、updated_at、deleted_time |
| knowledge_document_versions | id、document_id、owner_id、source_id 或 source_text、content_hash、profile_id、processing_config、state、error_code、warnings、chunk_count、created_at、completed_at |
| knowledge_chunks | id、version_id、ordinal、text、locator、token_count、search_document、embedding vector(1024) |
| knowledge_jobs | id、owner_id、kind、target_id、state、attempt、lease_until、fencing_token、next_attempt_at、error_code、created_at、updated_at |
| knowledge_requests | owner_id、operation、request_key、request_hash、document_id、version_id、created_at |
| project_knowledge_bases | owner_id、project_id、knowledge_base_id、created_at |

knowledge_sources 仅保存知识原件，当前没有通用文件模块，不先建设通用文件平台。
任务 kind 仅 ingestion 和 cleanup；不构建通用任务框架。knowledge_requests 用于
创建/替换幂等，避免只给文档增加一个键而无法处理多次更新。

约束与索引：

- owner/kb/document/version/source 的关系使用必要的复合唯一键和复合外键，
  active/pending 必须属于同一文档；版本来源严格二选一。
- (version_id, ordinal) 唯一；同一版本最多一个 ingestion 任务；清理任务同目标唯一。
- 请求键按 (owner_id, operation, request_key) 唯一，相同键不同 hash 返回冲突。
- 项目关联主键 (project_id, knowledge_base_id)，双方 owner 一致。
- 活动知识库、文档列表的 owner/更新时间索引，chunk 的 version 索引与全文 GIN，
  queued/retry 任务的 next_attempt_at 索引、processing 的 lease_until 索引。
- locator、processing_config、warnings 使用显式 Pydantic 类型及 JSON 序列化边界。
- 首版不建立 HNSW/IVFFlat；精确向量检索保证权限范围内完整选取。后续实测再优化。
- 向量及全文字段允许处理期间未填完，但 ready 发布必须验证所有片段完整且 profile 一致。

迁移复用已有 vector 扩展管理，核查是否已安装；缺扩展时提供明确操作要求。
不修改或清空历史表。迁移升级和回退均在临时 schema 验证，回退不删除共享扩展。

## 5. 上传、幂等与原件存储

文件创建和替换走 multipart；手动文本走 JSON 专用端点，不让 DTO 模糊区分内容。
前端不需要也不能直接操作磁盘或提交服务器文件路径。

1. 鉴权，检查知识库/文档可见性及参数，校验请求幂等键。
2. 流式写入本地临时文件，累计字节与 SHA-256，超过限制立即停止。
3. 检查文件签名、Office ZIP 结构、加密/宏格式、解压大小及成员数等；不执行宏、
   公式、外部链接，不解压到客户端提供路径。
4. 服务端生成不可变 storage_key，临时文件在同一磁盘原子改名到最终位置。
5. 同一数据库事务插入原件记录、文档/版本、任务和幂等结果；替换需锁定文档。
6. 提交后返回 202。事务失败尝试清理未引用文件；崩溃遗留由过期孤儿扫描清理。

磁盘与数据库没有共同事务：扫描只能删除超过宽限期、无记录引用的文件，不能
误删尚在提交中的上传。建议宽限期 24 小时，上传超时小于该值。内部暂存文件也
按年龄清理。禁止路径穿越及通过符号链接访问根目录之外的文件。

重复请求返回已有文档和版本，不重复排队；并发请求以数据库唯一约束仲裁，
落败者只清理自己的原件。相同内容替换可返回 no-op；跨用户不去重。
下载在 SQL 中检查 owner、知识库/文档删除状态与版本归属后打开文件，设置安全
下载文件名；文件缺失返回可识别错误，不伪装成业务资源不存在。

## 6. 解析、OCR 与切分

解析统一输出有序 ParsedBlock：text、locator、block_kind、warnings。
定位用 discriminated union：文本行号、Markdown 标题、PDF 页/区域、Word 段落/表格、
Excel sheet/cell_range、PPT slide/shape、图片区域。前端展示原始来源身份，不虚构页码。

- PDF 按页提取文本；低文本量或扫描页渲染 OCR。文字页中的独立图片区域也应评估
  OCR，避免“有文字就跳过整页图片”。同页按区域去重，保留 OCR 来源及警告。
- OCR 空结果只在整份资料无有效文本时判失败；识别异常、超时不能静默当作空图。
- DOCX 保留段落和表格顺序，遍历内部图片关系补充 OCR；不只依赖 inline shapes。
- XLSX 按工作表及行组提取，重复表头以辅助检索；合并单元格保留定位。公式不计算，
  有缓存值则采用并标注，缺缓存保留公式字符串并提示。限制实际扫描单元格数量，
  不盲信工作表声明范围；包内图片定位不可得时保留媒体身份及定位警告。
- PPTX 递归组形状，提取正文、表格与图片 OCR，来源关联幻灯片与 shape。
- 支持格式内的无法解析内容不能静默吞掉；核心内容无法读取则 failed，非核心对象
  不支持可 ready_with_warnings 表达于 ready 状态的 warnings 中，不再增加一套状态。

切分建议 512 个 embedding tokenizer token、重叠 64；保留标题和来源边界。
按段落累积，超长段落再切；表格按行组并重复表头。切分后的 text 就是 embedding
与引用使用的文本，locator 保留原始区间，不能以重排字符串偏移冒充原文定位。
所有参数写入版本处理 profile；更改规则需重建，不混用已存在索引。

解析/OCR 放到可终止子进程执行，限制并发、时间与内存，避免阻塞 API 与租约心跳。
取消 asyncio 等待不代表推理已停止：终止并回收子进程后才释放容量；本地 embedding
若使用不可中断线程，底层完成前继续占用推理槽位。正常关闭等待资源退出。

## 7. worker、状态与并发

版本状态 queued → processing → ready/failed；文档 active 与 pending 分开。
成功 ready 后 pending 清空；失败也释放 pending，但保留失败版本可查询。
重试该版本必须锁文档：没有其他 pending，且没有更晚的内容版本已创建，才允许
重新置 pending/queued；否则 409 version_superseded，避免旧失败版本覆盖新内容。

任务使用 SELECT FOR UPDATE SKIP LOCKED 认领，递增 fencing_token 并设置租约。
建议租约 120 秒、心跳 20 秒，采用数据库时间，心跳独立短事务；过期任务可重领。
全局 worker 并发与 owner 并发均受数据库准入控制：认领时锁 owner 对应 users 行
并检查未过期活动任务数，防止多 worker 绕过每用户上限；不持锁等待解析。

每次片段写入及发布，在短事务内锁定并校验任务 token/租约、知识库及文档可见性。
统一锁顺序：知识库 → 文档 → 任务；批量片段写入也遵守该顺序。
旧 worker 在租约失效后不可写；新 worker 取得 token 后清理未发布片段再重建。
完成时同一事务验证 chunk_count、字段、profile 与当前 pending，然后切换 active、
清空 pending、完成任务。解析、OCR、模型等待全部位于该事务之外。

瞬态 I/O/模型资源异常最多自动尝试 3 次，退避 5/30/120 秒并加抖动；格式错误不
自动重试。手动重试重置次数但仍执行版本防回退检查。数据库不可用时停止提交，
租约恢复后重新认领，不凭内存状态宣告成功。

文档删除与发布使用相同锁顺序；知识库删除先锁库并软删除、移除项目关联及创建
cleanup 任务，不对所有文档执行同步大事务。所有读与发布均检查库删除状态。
cleanup 批量删除派生数据和原件，幂等可恢复；磁盘删除失败任务保持可重试状态。
历史版本保留用于引用，只有显式删除文档/库时回收，不立即清理被替换的版本。

## 8. 检索流程与正确性

输入 SearchRequest：可信 owner、knowledge_base_ids、query、limit、context_budget。
所有指定库先确认同 owner 可见，任一不可见统一 404，不悄悄扩大范围。

1. 规范化 query，按与入库相同规则分词；保留原 query 供语义模型使用。
2. 两路 SQL 均 JOIN 未删除库、未删除文档及 active_version，随后才排序和 LIMIT。
   首版采用物化候选范围的精确向量距离排序，禁止全库 Top-K 后过滤。
3. 关键词生成受控 OR tsquery，按 ts_rank_cd 排序；不直接把用户字符串插入 tsquery。
   不用 plainto_tsquery 将全部搜索扩展词强制 AND，避免中文长查询过度收紧。
   精确编号额外保留整项 token，原始片段按边界精确匹配可提升同分结果。
4. 每路建议召回 40，按 chunk ID RRF 融合；相同版本相邻片段合并后按 token 截断，
   默认返回 8、最大 20、默认总预算 4000 tokens，保留合并后的所有 locator。
5. 响应前重新授权命中片段，已删除或已替换的内容剔除；并发发布造成不足时最多
   重查一次。返回各通道是否成功及 degraded_reasons，不输出伪造的置信概率。

两路使用独立事务，单路失败不污染另一事务；数据库整体不可用时返回 503。
embedding 不可用可仅关键词，关键词故障可仅向量；两路均失败返回 503，正常空
结果返回 200 空 hits。模型配置不匹配不得继续使用旧向量；返回向量通道不可用。

语义最近邻不等于资料足以回答问题。本次搜索不做“有答案”的保证，也不强制
用未经标定的阈值过滤；无关键词但语义近邻可返回，响应不宣称证据充分。
检索证据是资料数据，不作为系统指令；本轮不注册 MCP/内置工具。

## 9. HTTP 契约

前缀 /api/v1，全部复用 Cookie 身份；写请求同源校验、严格 DTO：

| 方法与路径 | 输入与结果 |
| --- | --- |
| POST/GET /knowledge-bases | 创建 201；分页列表 |
| GET/PATCH/DELETE /knowledge-bases/{kb} | 详情、名称说明修改、幂等软删除 204 |
| POST /knowledge-bases/{kb}/documents/upload | multipart file/title + Idempotency-Key，202 |
| POST /knowledge-bases/{kb}/documents/text | JSON title/text + Idempotency-Key，202 |
| GET /knowledge-bases/{kb}/documents | 分页、状态筛选 |
| GET/PATCH/DELETE /knowledge-bases/{kb}/documents/{doc} | active/pending/最近失败状态、改标题、删除 |
| POST /knowledge-bases/{kb}/documents/{doc}/versions/upload | 文件替换，202 |
| POST /knowledge-bases/{kb}/documents/{doc}/versions/text | 文本替换，202 |
| GET /knowledge-bases/{kb}/documents/{doc}/versions | 历史版本及状态 |
| POST /knowledge-bases/{kb}/documents/{doc}/versions/{version}/retry | 失败重试，202 |
| GET /knowledge-bases/{kb}/documents/{doc}/versions/{version}/source | 下载原件；手动文本为 TXT |
| POST /knowledge-bases/{kb}/search | query/limit，单库检索测试 |
| GET /knowledge-bases/{kb}/chunks/{chunk} | 历史版本片段详情，仍需检查文档/库可见 |
| GET /projects/{project}/knowledge-bases | 当前项目关联 |
| PUT/DELETE /projects/{project}/knowledge-bases/{kb} | 幂等关联/解绑 |

通用应用服务支持多库检索，暂不为尚无消费者的多库 HTTP 接口增加重复入口。
导入结果返回 document_id/version_id/state/status_url；详情显示 active_version、
pending_version、latest_failed_version，不能把更新失败描述成整份文档不可用。
搜索响应为 hits、channels、degraded_reasons；hit 含 chunk_ids、文档版本、标题、
正文和 locators；不返回向量、文件物理路径及内部错误。

400/422 参数非法；413 超限；415 不支持的类型；404 资源不可见；409 更新处理中、
幂等键冲突或版本已过时；429 配额/准入受限；503 存储或检索不可用。
将数据库、磁盘、模型错误分别映射，不扩大现有全局 OSError 处理器的吞错范围。

## 10. 项目接入与删除一致性

新增 ProjectKnowledgeService，依赖项目公开可见性用例与知识库公开可见性用例。
其持久化端口负责项目关联事务，核心知识库不反向导入项目领域。
关联创建与删除项目/库使用一致锁顺序：项目 → 知识库 → 关联。知识库删除无需
再锁项目，仅持有库锁并删除关联；删除项目先锁项目后删除关联，不能形成反向等待。
跨 owner 请求先确认项目可见，再检查库，避免利用错误优先级泄漏资源。

保留“项目存在 Session 不可删除”的规则；项目删除成功只移除绑定，不删知识库。
解析项目默认检索库通过外层公共方法完成，本次不接入尚未实施的 Run 流程。

## 11. 配置、资源与运维默认值

统一 ATHENA_KNOWLEDGE_* 前缀：ENABLED、STORAGE_ROOT、EMBEDDING_MODEL/REVISION/
DEVICE、OCR_MODEL_DIR、WORKER_CONCURRENCY、OWNER_CONCURRENCY、各类 LIMIT。
不复用 ATHENA_MEMORY_* 作为知识配置来源；相同模型可以在组装时复用实例。
profile 包括模型实际 revision、维度、距离、分词词典/规则版本、切分规则及解析版本。

建议默认：文件 20 MiB、PDF 500 页、图片 40MP、解压 200 MiB/10000 个成员、
扫描单元格 100 万、总提取文本 1000 万字符；worker 全局 2、每用户 2；
单用户原件 1 GiB、文档 1000 份。达到限额明确拒绝，不截断后假报完整导入。
每阶段有超时，解析/OCR 总计 10 分钟为初始值，可配置；上线前用真实样本调优。

多 worker 配额以数据库为准；上传最终提交时在 owner 锁内复核已用配额，临时磁盘
另设总容量与并发限制。更新期间新旧文件同时计费。清理完成后才释放实际占用。
日志只记录 IDs、耗时、状态与错误码，不记录正文、向量、凭据和私有文件路径。
度量排队时间、各阶段时间、租约重领、失败重试、检索耗时与降级率。
API 和 worker 使用同一持久化目录及数据库；备份需同时覆盖数据库和原件目录。

## 12. 分阶段工作与退出条件

| 阶段 | 工作 | 必须完成的验证 |
| --- | --- | --- |
| K0 | 技术样例与依赖锁定，固定模型 revision | 真实中文 OCR、三种 Office、pgvector/全文索引可运行；提交兼容版本清单 |
| K1 | 领域模型、迁移、知识目录、鉴权、原件上传下载 | owner 隔离、路径安全、幂等与磁盘/数据库失败恢复 |
| K2 | 任务认领、心跳、租约与版本发布 | 两 worker 争抢、租约过期、旧 worker 迟到、删除与发布竞争 |
| K3 | 所有解析器、OCR、切分与 embedding | 每种格式及边界样本，真实扫描 PDF/图片/Office 内图；替换失败保留旧版 |
| K4 | 全文、向量、融合与检索 API | 权限前置、真实中文模型、精确编号、降级、删除和更新可见性 |
| K5 | 项目集成、配额、清理、文档与整体回归 | 关联/删除竞争、恢复、API 闭环及既有模块回归 |

阶段拆分不降低本次 OCR/Office 范围。K0 不是只写空接口，K5 前不得宣称功能完成。

用户确认本方案后，由 luna-implementer 实施；完成后 terra-reviewer 独立检查实际
Diff，Root 验证结果。发现有效问题复用原 Implementer 修复后重新 Review，最多
3 轮修复，不跳过审查。当前没有修改应用代码，也没有因写本文启动实施 Agent。

## 13. 测试与最终验收

模块测试使用假存储、假时钟、假模型验证业务边界，不用它证明真实检索/OCR 效果。
真实 PostgreSQL 临时 schema 覆盖迁移、GIN/pgvector、复合外键、并发和任务恢复；
SQLite 不替代这些测试。模型/解析实测使用无敏感内容的固定本地样本。

建议新增 test_knowledge_service/api/storage/parsers/worker/search/project_binding 及
integration/test_knowledge_postgres、test_knowledge_jobs_postgres。关键用例：

- 登录和同源检查；所有文档/版本/片段/下载的跨 owner 和已删除资源一律不可读。
- 上传中断、重复并发、提交失败、孤儿扫描和清理任务重试均不误删有效原件。
- 进程强制退出后任务可恢复；过期 worker 不写入；失败重试不回退到历史内容。
- 混合文字/扫描 PDF、中文图片、Word 表格与图片、Excel 缓存公式/合并格/图片、
  PPT 组形状和图片能提取并定位；旧格式、加密、损坏、超限文件给出明确错误。
- 向量、关键词使用相同有效范围；大量其他用户高相似片段不影响本人 Top-K。
- 单路故障降级、全部故障 503；资源关闭与取消不泄漏推理容量或连接。
- 新版成功前旧版可检索，失败继续可用，成功后普通检索仅新版；历史来源仍可查看。

固定不少于 30 个正向问题，覆盖中文词句、语义改写、代码/编号及 OCR/Office 来源，
建议预期文档 Hit@8 ≥ 90%，所有精确编号样例命中；阈值作为本方案建议验收线。
另设无关查询样例，检查不伪造证据充分性；不能把语义近邻返回理解为错误回答。
记录测试机器、模型 revision、语料规模、冷启动/暖查询 p50/p95 与失败情况，
不把单机样例性能当生产 SLA。

最终运行 pytest、ruff check、ruff format --check、mypy，以及 PostgreSQL 和真实
模型/解析样例；说明跳过项与原因。全部必要测试和独立 Review 通过才宣告完成。

## 14. 实施准备判断

产品范围已足够形成完整计划，无需继续逐项询问实现参数。本文建议的 jieba、
PaddleOCR、解析依赖、图片范围、默认配额和验收线需随计划确认；K0 负责验证
环境可用性，不能预先宣称依赖和模型已就绪。用户确认后可开始实施 K0–K5。
