# PR 05：面向文件类型的专用检索

## 1. 文档状态

- 状态：待评审
- 前置依赖：PR 01～PR 03
- 阻塞范围：PR 06、PR 07
- 运行时行为变化：按 Adapter 独立启用

## 2. 背景

Athena 已经提取了大量结构化信息：

- PDF：页码、OCR 标记和表格。
- Excel：Sheet、公式、行数据和列级分析。
- 代码：Symbol、Signature、行号和依赖关系。

但 `FileIntelligenceRuntime.search_file()` 仍把所有附件统一转换成 Token Chunk，再执行 FTS5 + Vector RRF。通用切块可能把 Excel 数据行与表头分开、把代码 Symbol 截断，并产生不准确的行号或行范围。已有结构化索引没有进入通用搜索主链路。

本 PR 保留统一文件工具，在内部根据受信任的 `adapter_name` 路由到专用 Retriever。

## 3. 目标

1. 根据附件 Adapter Metadata 选择检索策略。
2. 为 Code、PDF、Excel 建立结构保持型 Chunk。
3. 将 Symbol、Dependency、Table 和 Locator 作为正式检索路线。
4. 所有结果提供稳定、准确的引用位置。
5. 保持现有工具 API 和 Session 隔离。
6. 为历史附件提供显式、可续跑的 Reindex。

## 4. 非目标

- 不合并 File Index 和 Memory Index。
- 不让 LLM 执行任意 Python、SQL 或表格公式。
- Reranker 由 PR 06 实现。
- 不自动把整个文件正文注入上下文。

## 5. 架构

```text
search_file(file_id, query)
        |
require_attachment(session_id, file_id)
        |
QueryAnalyzer + Trusted Adapter Scope
        |
FileRetrieverRegistry
  +-- CodeRetriever
  +-- PdfRetriever
  +-- ExcelRetriever
  +-- GenericTextRetriever
        |
SearchResult[] + Locator + Score Components
```

Registry Key 必须来自数据库中的 `attachment.adapter_name`，不能由文件名或模型输出决定。

## 6. 统一 Retriever 协议

```python
class FileRetriever(Protocol):
    adapter_names: frozenset[str]

    async def retrieve(
        self,
        attachment: Attachment,
        plan: RetrievalPlan,
        limit: int,
    ) -> list[SearchResult]: ...
```

所有结果统一携带：

```python
metadata = {
    "attachment_id": "...",
    "adapter_name": "code",
    "index_version": "code-v2",
    "kind": "symbol",
}
locator = {
    "path": "src/auth.py",
    "start_line": 42,
    "end_line": 67,
}
```

外部工具响应继续保留 `query/results`，新增 `retrieval_mode` 和分数明细属于兼容性扩展。

## 7. Generic Text Profile

Text 和 Word 暂时保留 Token Chunk，但做以下修复：

- 优先按段落/标题边界切分，再使用换行边界。
- Chroma Metadata 写入 Adapter Metadata，而不只保存 Locator JSON。
- 增加 `parent_id/kind/index_version`。
- 重叠区域只用于后续相邻扩展，避免多个近重复 Chunk 同时进入上下文。
- 未识别 Adapter 必须回退到该 Profile。

## 8. Code Retriever

### 8.1 索引

复用现有 AST/tree-sitter Symbol Index：

- 可靠行范围内，一个 Class/Interface/Function/Method 对应一个 Chunk。
- 索引文本包含 Signature 和 Docstring。
- 保存 Qualified Name、Simple Name、Language、Path、Start/End Line、Kind。
- 仅为架构类问题建立 File-level Summary Chunk。
- Dependency Edge 继续保存到现有依赖表。
- Parser 失败时回退为带准确行号的 Line Window。

Code Chunk 必须保存实际 `start_line/end_line`，不能只用字符偏移或统一 `start_line=1`。

### 8.2 路由优先级

1. Qualified Name Exact。
2. Simple Symbol Exact。
3. Symbol Prefix。
4. Path/Signature Keyword。
5. 引用、调用类问题走 Dependency Graph。
6. 行为或概念问题走 Code Vector。
7. Generic BM25 Fallback。

`find_symbols()` 增加 `match_type`，排序必须为 Exact、Prefix、Contains。Contains 仅作兼容回退，不能超过 Exact。

### 8.3 依赖问题

“谁调用、引用、depends on、call chain”等映射到 Graph Route。Graph Node 需要解析回 Definition Locator 后才能作为代码证据；未解析 Node 只作为诊断 Metadata。

## 9. Excel Retriever

### 9.1 Row Window Chunk

不再按整张 Sheet 文本任意切片，改为：

```text
Sheet 名称
Column Header
当前窗口的数据行
```

每个窗口重复 Header，并保存：

```text
sheet, start_row, end_row, columns, column_types, has_formulas
```

默认窗口 40～100 个非空行，同时受全局 Token Limit 限制。单个超长行不得截断成语义不完整的多行，需标记 `oversized`。

### 9.2 检索路线

- Sheet、Cell Reference：Exact。
- Literal Value/Identifier：Exact + BM25。
- “哪个 Sheet 包含……”：可对 Sheet Summary 做 Vector。
- 所有结果返回 Sheet 和 Row Range。

### 9.3 结构化计算

筛选、聚合、排序和分组使用受限 `TableQueryPlan`：

```python
@dataclass(frozen=True)
class TableQueryPlan:
    sheet: str
    operation: Literal["filter", "sum", "avg", "min", "max", "count", "top"]
    value_column: str | None
    group_by: tuple[str, ...]
    filters: tuple[TableFilter, ...]
    limit: int
```

约束：

- 仅执行 Allow-list Operation。
- Sheet/Column 必须在 Workbook Metadata 中存在。
- 使用 pandas/openpyxl API，不允许 `eval`、生成式 Python、任意 SQL 或 Macro。
- 现有 Table Artifact 只保留预览行，聚合必须读取可信原文件或完整版本化 Artifact，不能基于 201 行预览计算。

## 10. PDF Retriever

### 10.1 Parent-child

- Page 是强制 Parent Boundary。
- 能可靠提取时再增加 Heading/Section Parent。
- Child Chunk 用于检索。
- Parent Page/Section 仅用于后续上下文扩展。
- 每个结果保留 Page 和可选 Section Title。

首版可以只保证 Page Parent，不应把启发式 Heading 宣称为可靠结构。

### 10.2 Table

每个 PDF Table 单独索引：

- 推断成功时重复 Header。
- 按 Row Window 切块。
- 保存 Page/Table Number。
- Header 或 Cell 模糊时记录 Extraction Quality。
- 原始 Table Artifact 继续用于精确输出。

### 10.3 OCR

OCR Chunk 保存 `extraction_source="ocr"` 和可选 Quality。相关度相同时 Native Text 优先，但不能仅因 OCR 质量较低就全部丢弃。

## 11. Repository 与 Vector Metadata

扩展 Port：

```python
search_chunks(..., locator_filters=None, kinds=None)
find_symbols(..., match_modes=("exact", "prefix", "contains"))
get_adjacent_chunks(chunk_id, before=1, after=1)
```

Chroma 中保存可过滤的 Scalar Metadata：

```text
attachment_id, adapter_name, kind, language, page, sheet, path,
parent_id, index_version
```

完整 Locator JSON 继续用于响应重建，高频过滤字段额外扁平化存储。

## 12. Index Version 与 Reindex

在 Attachment Metadata 记录：

```json
{
  "retrieval_index": {
    "profile": "code-v2",
    "chunker_version": "2",
    "vector_version": "legacy"
  }
}
```

旧附件在显式 Reindex 前继续使用 Generic Profile。Reindex Task 必须：

1. 不删除 Active Index，先生成新版本 Unit。
2. 写入版本化 Chunk 和 Vector。
3. 校验 Count 与抽样 Query。
4. 原子切换 Attachment Profile。
5. 异步清理旧版本。

任务必须幂等、可中断续跑。

## 13. 文件改动

```text
athena/core/files/retrievers/base.py
athena/core/files/retrievers/generic.py
athena/core/files/retrievers/code.py
athena/core/files/retrievers/pdf.py
athena/core/files/retrievers/excel.py
athena/core/files/runtime.py
athena/core/files/adapters.py
athena/core/files/base.py
athena/core/files/tasks.py
athena/models/file.py
athena/infrastructure/sqlite/file_repository.py
athena/infrastructure/sqlite/models.py
athena/config/settings.py
tests/test_file_retrievers.py
tests/test_file_intelligence.py
```

## 14. 配置

```python
file_typed_retrieval_enabled: bool = False
file_code_symbol_candidate_k: int = 30
file_excel_row_window_size: int = 60
file_pdf_child_tokens: int = 400
file_pdf_parent_expansion: bool = True
file_retrieval_index_version: str = "typed-v1"
```

## 15. 测试与验收

测试必须覆盖：

- Python/TypeScript Definition 返回准确行号。
- Exact Symbol 高于 Prefix/Contains。
- Parser 失败走 Line Window。
- Excel Window 重复 Header 并保留 Row Range。
- Excel Aggregate 使用完整数据且结果精确。
- 非法 Table Operation/Column 不执行任何代码。
- PDF Child 保留 Parent Page。
- PDF Table 保留 Table Number 和原始行。
- OCR 与 Native Text 来源可区分。
- 所有 Retriever 先验证 Session Access。
- Legacy Attachment 仍可检索。

验收门槛：API/Class/Function Hit@3 至少 95%；Excel 计算 Fixture 全部正确；所有标注 PDF/Excel/Code 结果 Locator 正确；Reindex 中断后可安全恢复。

## 16. 发布与回滚

按 Code、Excel、PDF 顺序逐类开启 Shadow。Active Profile 由 Attachment Metadata 决定。回滚只切回旧 Profile，旧 Chunk 在回滚窗口结束前保留。

## 17. 风险

| 风险 | 缓解措施 |
| --- | --- |
| Chunk 数增加导致索引膨胀 | 版本化计量、限制窗口、切换后清理。 |
| Parser 行范围不准确 | 记录 Parser Confidence，回退 Line Window。 |
| 大 Workbook 成本高 | Read-only/Streaming、限制输出、后台 Artifact。 |
| PDF Heading 启发式误判 | V1 只承诺 Page Parent。 |

