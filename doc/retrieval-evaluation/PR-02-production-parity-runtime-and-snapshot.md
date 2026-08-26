# PR-02：用真实生产流程运行检索评估

## 先说结论

PR-01 解决了“标准答案和评分规则是什么”。本 PR 解决“怎样用真正的生产流程来答题”。

它会创建一个和生产环境使用相同解析器、切分逻辑、Embedding、索引和检索入口的评估运行时，但把所有数据放在独立目录中。评估过程可以真实地从文件上传开始，一直走到解析、切分、建索引和检索，结果才有生产参考价值。

## 1. 状态和边界

- 状态：待评审
- 前置依赖：PR-01
- 后续 PR：PR-03、PR-04
- 生产默认行为不变，只新增隔离的评估命令

## 2. 为什么需要真实运行时

如果评估器仍然直接调用 fixture，得到的分数只能说明“测试替身表现如何”，不能说明生产系统表现如何。

本 PR 会建立一个评估 Runtime，从脱敏原始文件开始，执行真实的：

1. 上传和保存。
2. 文件解析和 OCR。
3. 文档切分。
4. Embedding。
5. FTS/Chroma 建索引。
6. memory/file 检索。

评估 Runtime 和生产程序同构，但使用单独的数据目录、数据库和索引。

## 3. 设计原则

- 复用生产的依赖注入和配置解析，不复制一套检索算法。
- 每次评估使用独立 workspace，禁止读写用户默认的 `data/`。
- `prepare` 阶段可以写入评估目录；`run` 阶段只能读。
- 评估失败时，要明确区分“索引准备失败”和“检索没有召回”。
- 把输入数据、模型、解析器和代码版本写进 snapshot manifest，确保以后知道这次分数是怎么来的。

## 4. 模块和接口

```text
athena/evaluation/
├── runtime.py             # 创建 EvaluationRuntime
├── snapshot.py            # 管理 manifest、目录、锁和清理
├── ingestion.py           # 执行真实上传、解析和建索引流程
├── bindings.py            # 把别名和 locator 对应到真实 ID
├── executor.py            # 只读调用 memory/file 检索
└── readiness.py           # 检查索引是否完整可用
```

核心接口：

```python
class EvaluationRuntime(Protocol):
    async def prepare(self, dataset: DatasetManifest) -> SnapshotManifest: ...
    async def retrieve(self, case: EvalCase) -> RetrievalOutcome: ...
    async def close(self) -> None: ...
```

实现必须复用以下生产组件：`SqliteMemoryRepository`、`ChromaMemoryStore`、`MemoryManager`、`HybridRetrievalManager`、`FileRepository`、`FileIntelligenceRuntime`，以及生产配置中的真实 LLM Provider。

实现禁止导入 `tests.retrieval_eval.fixtures`。

## 5. 评估 workspace

每次运行使用类似下面的独立目录：

```text
.run/retrieval-evaluation/<run_id>/
├── athena.db
├── chromadb/
├── files/
├── bindings.json
├── snapshot-manifest.json
├── logs/
└── lock
```

启动时必须显式把生产配置改到这个 workspace：

```text
SQLITE_DB_PATH=<workspace>/athena.db
CHROMADB_PATH=<workspace>/chromadb
FILE_STORAGE_PATH=<workspace>/files
```

如果发现配置仍指向生产目录，命令必须拒绝启动。workspace 使用锁文件，防止两个任务同时写同一个索引。

snapshot manifest 至少记录：运行 ID、数据集 ID 和版本、SQLite/Chroma hash、Embedding/LLM 模型、解析器版本、application commit，以及 settings hash。

## 6. 如何准备真实数据

### 6.1 Memory

从脱敏的 `memories.jsonl` 读取数据，并通过 `MemoryManager.add_memory()` 写入。

同时保存 `memory_alias` 到实际生成 ID 的对应关系。

不能直接插入 SQLite 或 Chroma，否则会跳过生产中的 dual-write、metadata 和 FTS 行为，评估结果就不完整。

### 6.2 文件

对 corpus 中每个文件执行与 REST 上传等价的流程：

1. 创建隔离 session。
2. 通过 `StorageLayer.save_stream()` 保存文件。
3. 创建 attachment。
4. 调用 `parse_attachment()`。
5. 调用 `index_attachment()`。
6. 等待附件状态变成 `READY`。

解析、OCR、代码分析或表格抽取失败时，必须写入 readiness report。评估器不能只调用 `search_file()`，却跳过解析和建索引。

### 6.3 索引完整性检查

`readiness.py` 至少检查：

- 所有附件都存在，并且状态为 `READY`。
- 每个标准答案 locator 至少对应一个 chunk。
- SQLite FTS 和 Chroma collection 都有对应记录。
- 代码 symbol、Excel sheet/row、PDF page 等位置信息没有丢失。
- 文件 sha256 与 manifest 中记录的一致。

关键文件失败时，`prepare` 必须返回非零退出码，并且不能生成可以用于 gate 的报告。

## 7. 评估时如何保证只读

当前 `MemoryManager.search()` 和 `keyword_search()` 会记录访问统计。需要新增可选参数 `record_access: bool = True`：

- 生产默认仍为 `True`。
- 评估和 Shadow 强制使用 `False`。

这样评估不会改变记忆的热度、TTL 或用户数据。

`RetrievalTrace` 增加以下可选字段：`evaluation_run_id`、`evaluation_case_id`、`dataset_version`、`index_version`。

memory 和 file executor 都必须记录 trace，包括候选结果、最终结果、fallback、各阶段延迟和 locator。评估器不读取候选正文，只用真实返回的 ID/locator 和 PR-01 的标准答案进行匹配。

## 8. 命令行

```bash
python -m athena.evaluation.cli prepare \
  --dataset retrieval_eval/datasets/production-sanitized-v1 \
  --workspace .run/retrieval-evaluation/eval-001

python -m athena.evaluation.cli inspect \
  --workspace .run/retrieval-evaluation/eval-001
```

相同数据集版本、settings hash 和 workspace 重复执行 `prepare` 时应保持幂等。需要强制重建时，必须使用新的 workspace。

## 9. 文件改动

```text
athena/evaluation/runtime.py
athena/evaluation/snapshot.py
athena/evaluation/ingestion.py
athena/evaluation/bindings.py
athena/evaluation/readiness.py
athena/core/memory/memory.py
athena/core/retrieval/trace.py
tests/test_evaluation_snapshot.py
tests/test_evaluation_readiness.py
```

## 10. 测试和验收标准

- 使用真实的临时 SQLite、Chroma 和文件目录完成 PDF、Excel、代码和 memory 数据准备。
- 删除或损坏文件时，readiness 必须失败，并且不能生成可发布报告。
- 相同 snapshot 重复运行时，绑定关系和检索 ID 顺序稳定。
- `record_access=False` 不改变 memory 的访问统计和 TTL。
- 评估 workspace 不能访问默认生产目录。
- 至少一条用例通过真实的 `HybridRetrievalManager`，至少一条通过真实的 `FileIntelligenceRuntime`。

## 11. 风险和回滚

真实模型和 OCR 可能耗时较长，也可能存在非确定性。通过固定模型配置、记录版本、限制并发和设置超时来降低影响。

评估 workspace 可以整体删除；本 PR 不迁移、不修改生产数据，因此不会影响线上系统。
