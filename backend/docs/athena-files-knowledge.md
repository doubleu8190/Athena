# 文件智能与知识库实现

依赖：[系统实现总说明](athena-system-overview.md)、[运行时与数据底座](athena-runtime.md)。被 [HTTP API 与事件协议](athena-api.md)、[工具/审批/MCP](athena-tools-approval-mcp.md)、[记忆/检索](athena-memory-retrieval.md)、[前端复刻](athena-frontend.md) 依赖。

## 1. 资源与权限

Attachment 是文件原始 blob、解析状态和派生索引的统一记录。字段包括 id、logical_document_id、document_version、session_id 或 knowledge_base_id、message_id、filename、mime_type、size_bytes、sha256、adapter_name/version、status、capabilities、metadata、error_message、created/updated/deleted_time。`storage_key` 只存数据库和内部运行时，不出现在 API。

附件可以属于一个会话，也可以属于一个独立知识库。文件工具通过 `require_attachment(session_id,file_id)` 校验：直接属于当前会话，或属于全局知识库且已经 ready；否则视为不存在。删除均为软删除。

知识库记录 name、description、文档数量、ready 数量、总字节数和时间；同一文件名再次上传时复用 `logical_document_id` 并递增 `document_version`，版本可查询。

## 2. 支持格式和适配器

AdapterRegistry 启动时注册 PdfAdapter、WordAdapter、ExcelAdapter、ImageAdapter、CodeAdapter、TextAdapter。按扩展名优先，再按 MIME 类型和 guessed MIME 选择；`.doc/.xls` 明确拒绝并提示转换为 `.docx/.xlsx`；扩展名和 MIME 不匹配返回 415。

| 适配器 | 输入 | 抽取和分析 |
| --- | --- | --- |
| text | txt、md、json、csv 等 | 解码文本；CSV 提取 headers/前 200 行表格，文本记录行数 |
| pdf | pdf | 按页提取原生文本；混合页可识别嵌入图片 OCR；保留页 locator |
| word | docx | 段落、表格、标题和段落 locator；支持文档分析 |
| excel | xlsx | sheet、行列和表格数据；可用 pandas 做统计分析 |
| image | png、jpg 等 | OCR 文本、尺寸/模式；主 LLM 声明 supports_vision 时发送 base64 图片做视觉分析 |
| code | 常见代码扩展名/代码目录 | Tree-sitter 符号、语言、文件数、依赖和调用关系 |

每个适配器返回 `ExtractionResult(units, metadata, tables)`。unit 含文本、locator（page/sheet/path/line/char）和语义 metadata。

## 3. 上传和后台处理

保存流程：

1. 清理文件名路径，只保留 basename，拒绝空名/NUL。
2. AdapterRegistry 校验类型。
3. StorageLayer 以 SHA-256 内容寻址，流式按 1 MB 读取，超过 `FILE_MAX_UPLOAD_BYTES` 返回 413。
4. 写 attachments，初始状态 uploaded；知识库/会话上传都创建持久 `knowledge_document_jobs`。
5. 返回 202；worker 每秒轮询 queued/retry jobs，最多 5 次，支持进程重启恢复 interrupted 状态。
6. `process_knowledge_document()` 对同一附件加锁：状态 processing → adapter extract → chunk → 写 PostgreSQL chunks/artifacts → 写 pgvector → knowledge base 文档额外写 Neo4j 图 → 状态 ready。
7. 任何失败写 failed/error_message 并按可重试时间重入队；图索引失败不影响向量/关键词 RAG，结果中 graph.status=failed。

解析事件：`file_processing_started`、`attachment_updated`、`file_processing_completed`、`file_processing_failed`，都带 attachment_id 和统计/错误 payload。

## 4. 分块和索引

`FileChunker` 以 embedding token counter 估算 token，目标 `FILE_CHUNK_TOKENS=800`，重叠 `FILE_CHUNK_OVERLAP_TOKENS=80`；优先在段落、换行和语义边界切分，chunk ordinal 从 0 开始。chunk 写入 attachment_id、content、token_count、locator、metadata。

文件向量索引按 attachment_id replace；metadata 至少含 attachment_id、document_version、ordinal、locator_json。关键词使用 PostgreSQL FTS。删除文件先删除向量和图，再删除/软删除 chunk、artifact 和 attachment；只有无 live attachment 引用的 blob 才回收。

## 5. 文件读取和检索

`read_file` 对未 ready 文件返回 `waiting=true` 并发布 `agent.waiting_for_files`，failed 返回 error；ready 时按 page/sheet/path 过滤 chunk，最多 50 个。图片没有 OCR 文本时返回提示，建议使用 analyze_file。

`search_file` 流程：

1. 创建 retrieval_run，记录 scope=file、file_id、candidate_k、rerank_k、fusion=rrf、index_generation。
2. 关键词和向量各取候选。
3. 用 RRF `score += 1/(60+rank)` 合并相同 source_id。
4. 前 `FILE_RERANK_K` 交给 cross-encoder（不可用时回退 fused score）。
5. 返回前 `limit`，每条保留 keyword/vector/fused/rerank score/rank、locator、source_title、document_version、selected_for_result。
6. 把候选和完成状态写 retrieval_candidates/retrieval_runs；记录失败/取消。

知识库上下文使用多个文档的相同 pipeline，并按 `KNOWLEDGE_RERANK_CANDIDATE_K`、`KNOWLEDGE_RERANK_K`、`KNOWLEDGE_RESULT_K` 和每文档上限做去重/多样化。

## 6. 文件分析

- `extract_table` 读取 adapter 阶段缓存 artifact，不重复解析。
- `summarize_file` 先查 artifact cache；把 chunks 每 8 个一组交给副 LLM 做 chunk summary，多轮压缩到最多 8 个，再用主 LLM 生成最终 summary 并缓存。
- `analyze_file` 调 adapter analyze；图片有视觉能力时发送 data URL，否则只返回 OCR/尺寸能力说明。
- `analyze_codebase` 返回 languages/files/symbols/dependencies；`find_symbol` 查询符号；`find_definition` 是同一符号查询别名；`find_references` 使用 incoming；`get_call_graph` 支持 incoming/outgoing/both。

## 7. 复刻测试

必须测试扩展名/MIME 冲突、旧格式拒绝、超大文件、同名版本、worker 重试和重启恢复、解析失败补偿、未 ready waiting、页码/sheet/path 定位、RRF 候选轨迹、图片无视觉模型 fallback、删除后的向量/图/blob 清理，以及会话附件不能被其他会话读取。
