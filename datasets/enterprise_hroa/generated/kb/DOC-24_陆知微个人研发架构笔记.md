# 陆知微个人研发架构与带教工作笔记

- **文档密级标识**：`LU-PERSONAL-ARCH-202611`
- **归属人**：陆知微（`MOCK005` / `mock_rd_member` / 平台架构组 P7 架构师）
- **可见边界**：个人私有知识库 `KB-6`（`kb_lu_personal_notes`），仅本人可读，HR 成员苏晚晴（`mock_hr_member`）及超级管理员周明远（`mock_super_admin`）均不可访问。

## 一、`athena-core` 灰度发布个人备忘

1. **灰度批次规划（2026-12-03 窗口）**：
   - 第一批 `canary-5%` 仅切入上海张江内部体验流量，重点观测 `sse_stream_latency_p99 < 420ms` 与 pgvector HNSW 召回延迟；
   - 严禁在灰度观察未满 30 分钟前扩容至 `50%`；若 5xx 错误率连续 2 分钟超过 `0.5%`，立刻执行 `helm rollback athena-core --revision 42` 一键回滚。
2. **交接自查项（承接自韩景明 `XC1003`）**：
   - 已核对生产集群 `prod-sh-k8s-01` 的 PodDisruptionBudget 与定时备份 CronJob，待 `2026-12-09 18:00` 确认韩景明个人 SSH 与 `kubeconfig` 凭据彻底吊销。
   - 个人架构优化待办：12 月中旬整理 `athena-core` 上下文压缩观测指标看板（摘要命中率、Token 降幅、单次压缩耗时）。

## 二、新人陈思远（`XC1001`）首周带教大纲（2026-12-01 报到）

作为陈思远（高级后端工程师 P6，`OFFER-2026-1108`）的入职导师，个人私密带教节奏备忘如下：

1. **Day 1（12-01 周二）**：协助领取 `MacBook Pro 14 (M4 Pro / 36G / 1TB)`，跑通本地 Docker Compose、PostgreSQL pgvector 与 `.venv` 单元测试环境。
2. **Day 2–3（12-02 至 12-03）**：一对一串讲 `athena-core` 领域分层（`domain → app → adapter → api`）及 Run 状态机事件流设计。
3. **Day 4–5（12-04 至 12-05）**：分配首个热身 Issue（补齐单测与日志脱敏检查），完成首次 Code Review 并在周五 16:00 做首周成长反馈。
