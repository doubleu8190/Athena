# 记忆生命周期简化实施方案

日期：2026-11-15。状态：已实施，验证结果见验收报告。
依据：[设计文档](memory-lifecycle-design.md)。验证见[验收报告](memory-lifecycle-e2e-report.md)。

## 1. 目标

单 Memory Worker 顺序处理，删除分布式恢复协议，保留证据、权限、事实语义、单条版本、同步回执与请求前复核。

| 删除 | 替代 |
| --- | --- |
| scope revision CAS、多代次、时间屏障 | 用户状态行锁和一个 generation |
| 租约、心跳、lease_token、worker 归属 | 普通任务状态与单进程启动恢复 |
| manifest、fragment_id、逐片回执及持久化候选计划 | 完整 Run 有界批次，整批原子提交，超长 Run 跳过 |
| 竞争后三次重新决策 | 失效结果丢弃，下次调度再读取 |
| 事实身份字段、别名归一、事实组状态机和 SemanticReviewOutput | 候选正文检索 Top-K，一次批量 ADD/UPDATE/IGNORE 决策 |

## 2. 数据与迁移

- memory_settings 增加 generation，复用为用户写事务锁行。
- memory_scope_state 只保留范围启用和维护进度，移除 revision、mutation_epoch、extraction_not_before。
- memory_jobs 使用 queued/running/done/failed，删除租约和分片恢复字段；control 回执与扫描进度保留类型化 JSON。
- Run 保留整 Run 提取状态与固定上下文，删除 fragments，保留“已初始化但为空”。不保存额外输入代次。
- 新增增量迁移，兼容已升级旧生命周期结构的数据库；保留正文、来源、版本和回执，旧未完成任务重新排队。
- 不直接修改用户日常数据库，不以兼容包装保留旧协议。
- 新增 0027 删除事实身份列、组索引和约束；正文、来源、版本、回执和 0026 的 generation/恢复行为保持。

## 3. 应用和仓储

直接删除 lease/heartbeat/save-fragment/refresh-CAS 等端口、命令及调用。
后台只做：选未处理 Run → 读 generation → 联合提取 → 同范围向量/全文 Top-K → 一次批量决策 → embedding →
锁用户行 → 检查 generation/来源/成员 → 原子提交记忆与成员状态。
模型调用不持事务，generation 失效不在当前任务重做模型决策。

删除 fact_resolution.py、事实组端口和旧类型，不只是在新检索流程外保留备用分支。
提取结果仅为正文与证据等必要元数据；模型决策只能修改本次为该候选召回的旧 ID，版本由服务器固定。
ADD 保留并存，UPDATE 保留来源/替代关系，IGNORE 无写入。一次决策覆盖所有候选，不允许重复更新同一旧目标。
检索融合去重与输入预算有界；缺少召回不能用相似度直接覆盖。

同步 remember/correct 工具删除事实字段；remember 明确存正文，correct 需要 ID/version，forget 不变。
memory_search 返回可用于后续纠正的 ID/version。控制不额外调用语义评审模型。

同步控制在用户锁内重查回执、复核单条版本并提交变更与 generation。忘记、纠正、关闭提取及
来源失效还需保守跳过旧未处理输入；旧 Run 延迟终态和旧输入重试不得覆盖 skipped。
不新增第二套失效代次或逐事实删除追踪。
首次提交与恢复查询共用规范正文、证据及参数 SHA-256 摘要；后台 UPDATE 与同步 correct
共用最大证据时间计算，保留新证据、旧目标和复制来源中最新的时间。

## 4. Worker 和部署

入口只启动一个顺序循环，移除并发、租约续期和失租监督。启动恢复普通 running，不恢复 control。
已提交整批以成员 Run 状态去重，未提交事务回滚后重新读取。
保留唤醒、扫描、每日归档和 scope 清理，不建设通用任务平台。
清理 .env.example、Compose 与脚本中的 Memory Worker 并发/租约/心跳配置，更新 README。
不改普通 Run Worker 的执行租约或其他领域架构。

## 5. 验收

| 场景 | 必须观察的结果 |
| --- | --- |
| 提取期间用户纠正/删除 | generation 变化，整批旧结果不写入 |
| 旧 pending 或未终态 Run | 忘记后不由后续调度、延迟终态或重试恢复旧事实 |
| 提交失败 | 无半批记忆、来源或成员完成标记 |
| 提交成功但响应丢失 | 已处理成员不重复应用，不重复增加版本 |
| Worker 重启 | 普通 running 恢复，control 不执行 |
| control 并发重放 | 一次业务变更、同一回执，同键异参拒绝 |
| 超长单 Run | skipped 和安全原因，没有分片或截断 |
| 决策语义 | 苹果香蕉 ADD 并存、明确最喜欢变化 UPDATE；同批选最终状态、IGNORE 不写 |
| 检索与目标校验 | 向量/全文融合去重，scope 隔离，未检索 ID/重复目标/旧版本不写 |
| 模型调用 | 正常提取一次、批量决策一次，零候选不决策，无逐条二次评审 |
| 固定上下文 | 恢复不扩选，空快照不变，每请求复核 |
| 迁移 | 正文、来源、回执、固定上下文保留，旧协调列和分片内容移除 |

静态检查使用 ruff check backend/src backend/tests、mypy backend/src 和 git diff --check。
单元与集成分离环境运行；真实 PostgreSQL 使用隔离 schema，不打印凭据。
本次改变模型输入输出契约，需重跑真实文本模型专项；真实 embedding 与独立进程部署效果单列限制。
旧方案的测试数字不用于宣称本次通过。

## 6. 工作流

Root 协调和文档，Implementer 负责代码、迁移与测试，Reviewer 只读审查最终实际 Diff。
审查同时确认正确性和旧防线确实删除，不能仅改名搬移复杂度。有效问题回交修复并复审，最多三轮。
默认开关保持关闭，不自动部署或提交 Git。
