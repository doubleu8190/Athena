# PR-04：在线观察真实请求，并把问题变成新的标准答案

## 先说结论

离线评估可以比较版本，但不可能提前准备所有真实问题。生产环境中还会出现长尾 Query、不断变化的文件、权限范围、真实延迟和模型服务波动。

本 PR 增加一个默认关闭的 Shadow（影子）检索链路：

- 主链路照常为用户提供结果。
- Shadow 链路异步、只读地执行另一套检索。
- Shadow 只记录脱敏后的差异，不影响用户看到的回答。
- 经过授权的失败样本交给人工标注。
- 仲裁完成后，把样本按 PR-01 的格式加入新的金标数据集。

## 1. 状态和边界

- 状态：待评审
- 前置依赖：PR-01、PR-02、PR-03
- 默认关闭，只新增异步 Shadow 观测

## 2. Shadow 是什么

Shadow 可以理解为“旁路考试”：它会拿同一个用户请求再测试一次候选检索方案，但它的结果不参与用户回答。

```text
用户请求
   |
   +--> 主检索 -> 用户回答
   |
   +--> 采样器 -> 异步队列 -> Shadow 检索（record_access=False）
                              |
                        脱敏 trace / 差异事件
```

它可以帮助我们观察新旧检索方案在真实请求上的差别，而不必把实验结果直接暴露给用户。

## 3. 必须遵守的安全边界

Shadow 结果绝不能：

- 注入用户可见的回答。
- 改变主链路的排序或结果。
- 增加 Memory `access_count`，刷新 TTL，或写入新记忆。
- 触发文件解析、摘要、事实抽取、删除或索引迁移。
- 记录完整 Query、候选正文或敏感附件内容。

Shadow 默认关闭。只有内部环境或经过审批的生产采样才能开启。

## 4. 模块设计

```text
athena/evaluation/
├── shadow.py              # 采样、队列、超时、并发和丢弃策略
├── feedback.py            # 用户反馈和人工标注任务
├── redaction.py           # Query/结果脱敏和敏感信息检测
├── annotation.py          # 标注、仲裁和版本发布
└── storage.py             # 只保存 hash、ID、locator 和标注元数据
```

应用层新增 `ShadowRetrievalRunner`，由 `RuntimeContainer` 注入真实的 memory/file executor。

它必须使用主链路已经解析好的 scope，不能自行扩大 session 或 attachment 范围，否则会造成越权检索。

## 5. Shadow 如何运行

每个事件至少包含：event ID、request/query hash、source scope、baseline/candidate ID、排名变化、fallback、延迟、脱敏状态和时间戳。

候选正文不能落库。

默认配置示例：

```yaml
retrieval_shadow_enabled: false
retrieval_shadow_sample_rate: 0.05
retrieval_shadow_max_concurrency: 4
retrieval_shadow_timeout_ms: 5000
retrieval_shadow_queue_size: 1000
retrieval_shadow_drop_on_overload: true
```

配置含义：

- 默认不开启 Shadow。
- 开启后只抽样 5% 的请求。
- 最多同时运行 4 个 Shadow 任务。
- 单个任务最多运行 5 秒。
- 队列最多保存 1,000 个任务。
- 过载时丢弃 Shadow 任务，而不是拖慢主请求。

超时、队列已满或模型不可用时，只能丢弃 Shadow 任务，不能影响用户请求；同时必须增加计数并触发监控报警。

## 6. 没有标准答案时能看什么

如果还没有人工金标，Shadow 只能说明“两个方案的行为不同”，不能声称“某个方案准确率更高”。

可监控的指标包括：

- 主链路和 Shadow 候选集合的 Jaccard 相似度。
- 第一名结果变化率。
- Top-K 结果覆盖差异。
- 零结果率、fallback rate、索引缺失率。
- Shadow 的 P50/P95/P99 延迟和超时率。
- 按 memory、PDF、Excel、code 和 query label 分组的差异。
- 采样率、脱敏失败率、队列丢弃率和模型成本。

指标必须带上 dataset、index、model 和 application commit 标签，避免把不同版本混在一起。

## 7. 哪些样本进入人工标注

样本可以由以下情况触发：

- 用户明确反馈“没有找到”或“结果不相关”。
- 主链路和 Shadow 的 Top-1 差异超过阈值。
- 命中 `expected_empty` 风险规则或敏感结果规则。
- 运维人员从 trace 中手动选择失败样本。

## 8. 人工标注怎么做

标注界面只展示经过授权的脱敏 Query、文档别名、locator 和完成判断所需的最小片段。

标注人需要填写：

```text
relevance: 0 / 1 / 2 / 3
locator_correct: true / false / unknown
should_be_empty: true / false / unknown
error_stage: parse / index / candidate / fusion / selection / unknown
```

这些字段分别表示：相关程度、定位是否正确、是否应该为空，以及问题大概发生在哪个阶段。

每条任务至少由两名标注人独立完成；有冲突时进入仲裁。标注任务带有 `annotation_version`，已经发布的标签不能直接修改。

## 9. 如何回流到金标数据集

仲裁完成后，系统生成符合 PR-01 `EvalCase` 协议的新用例：

- 使用稳定的 document/memory alias 和 locator。
- 保存脱敏 quote hash。
- 记录样本来源、匿名化时间和审核者。
- 递增数据集版本。
- 触发 PR-03 的 nightly gate。

这样线上发现的问题会逐渐沉淀成可重复的离线测试，而不是只停留在一次性排查结果中。

## 10. 数据保留和权限

- 原始反馈内容默认不落库；获得批准后，才进入独立的加密存储，并设置 TTL。
- Shadow 事件保留 30 天。
- 聚合指标保留 180 天。
- 已发布的金标长期保留版本 hash。
- 只有评估管理员可以查看 locator 片段；普通开发者只能查看聚合报告。
- 所有导出、标注和门禁覆盖操作都写入审计日志。

## 11. 文件改动

```text
athena/config/settings.py
athena/evaluation/shadow.py
athena/evaluation/feedback.py
athena/evaluation/redaction.py
athena/evaluation/annotation.py
athena/evaluation/storage.py
athena/runtime.py
tests/test_retrieval_shadow.py
tests/test_evaluation_redaction.py
tests/test_evaluation_annotation.py
```

如果项目已经有反馈持久化层，应优先扩展现有 repository。评估数据不能写进用户会话消息表。

## 12. 测试和验收标准

- Shadow 关闭时，不产生额外检索调用；开启时，不改变主链路结果。
- Shadow 超时、队列满或发生异常时，都不能传播到用户请求。
- `record_access=False` 时，memory 访问统计不变化。
- 脱敏失败的事件不能入队，并且要有可观测计数。
- 标注、冲突仲裁、版本发布和回流 JSONL 的流程都可测试。
- 在预发环境完成至少一轮真实采样，并证明 Shadow 结果没有进入用户回答。

## 13. 发布和回滚

把 `retrieval_shadow_enabled` 设为 `false` 即可立即关闭 Shadow。关闭不会影响主链路，也不会删除已经保存的聚合指标。

人工标注协议通过版本号回滚；已经发布的金标数据不允许原地修改。
