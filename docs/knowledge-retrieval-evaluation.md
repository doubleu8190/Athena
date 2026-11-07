# 知识检索本地 fixture 评估

日期：2026-11-07

本次评估验证本地 BGE 向量和 Jieba 关键词名次经 RRF 合并后的排序。它在 Python
中直接对 fixture 语料计算，不会调用 PostgreSQL、GIN、pgvector 或 HTTP API，
因此不能视为 PostgreSQL 端到端质量、并发或延迟指标。

| 项目 | 结果 |
| --- | --- |
| 语料 | 16 条固定文档，覆盖数据库/编号、版本、worker、OCR、Office、存储与融合 |
| 查询 | 39 条，覆盖改写、术语、精确编号、OCR 与 Office 来源 |
| 模型 | `BAAI/bge-m3` revision `5617a9f61b028005a4858fdac845db406aefb181` |
| 向量 | 1024 维，有限且非零 |
| 平台 | Apple Silicon arm64，本地 CPU |
| RRF | 两路等权，常数 60 |
| Hit@8 | 39/39 (100%) |
| 精确标识符类命中 | 7/7 (100%) |
| 实测耗时 | 8.18 秒，包含 fixture 文档与查询向量生成及本地融合 |

运行命令：

```sh
KNOWLEDGE_REAL_EMBEDDING=1 .venv/bin/python -m pytest -q -s \
  backend/tests/test_knowledge_embedding_live.py
```

fixture 位于 `backend/tests/fixtures/knowledge_retrieval_eval.json`，gated 测试位于
`backend/tests/test_knowledge_embedding_live.py`。结果只对这 16 条人工构造语料
有效；上线前仍需扩大真实文档和困难负例，并在 PostgreSQL 实际过滤/索引路径上
测量命中率、延迟及跨用户数据不干扰。
