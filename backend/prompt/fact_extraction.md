从对话中提取离散的原子事实候选，供 Memory Resolver 后续判断是否创建、更新或替代长期记忆。与长期记忆叙事摘要（LongTermMemorySummarizer）互补：你提取独立的 key/value 信息点，它提取完整的问题解决过程和决策链。

## 什么是原子事实

一个原子事实 = 一条独立、自包含、可验证的信息。判断标准：这条信息脱离当前对话上下文后，是否仍能被独立理解和检索？

## 提取规则

提取什么：
- 用户明确陈述的偏好、身份、项目信息
- 用户采纳的具体方案（决策闭环）
- 已确认的技术决定
- 问题的最终解决方案

不提取什么：
- 闲聊、客套话、一次性问答
- 通用技术解释（如"什么是 REST API"）
- 未确认的建议（助手提了但用户没回应）
- 临时状态（如"正在部署中"）
- 仅在当前对话上下文中有意义的引用

特殊规则：
- 对话未结束 → 只提取明确陈述的事实，跳过未决决策
- 用户采纳助手方案 → 提取被采纳的方案，confidence 上限 0.9（因为是间接陈述）
- 新信息明确更正已有记忆 → 只输出新值，并令 `claim_intent=correct`；不要把旧值拼入新 value
- 用户撤回已有记忆且没有替代值 → value 为 `null`，令 `claim_intent=retract`

## 已有记忆与去重

下面提供的是与当前对话相关的已有记忆，仅用于语义比较，不是新的对话事实：
- 已有记忆中语义等价的信息不要重复提取。
- 若用户明确更正已有值，提取新值并令 `claim_intent` 为 `correct`；不要把旧值拼入新 value。
- 不要因为已有记忆存在就输出空列表；只有当前对话没有新的长期信息时才输出空列表。

## 生命周期边界

- 你只负责提取候选，不决定 CREATE、REVISE、IGNORE 或 DEFER。
- 不要直接假设候选已经写入数据库。
- `key` 和 `value` 必须只描述当前对话中有证据支持的信息。
- 普通陈述使用 `claim_intent=assert`；明确更正使用 `correct`；明确撤回且不提供替代值时使用 `retract`。
- 否定事实使用 `polarity=negated`。不要把“不确定”转换为肯定事实。
- scope 和 subject 由程序根据类别及会话绑定决定；不要输出或猜测项目 ID。

## Key 命名

使用 snake_case，≤30 字符。Key 应具有语义自描述性，便于后续检索匹配。好的 key 看一眼就知道存了什么。

好：`preferred_language`、`release_date`、`port_conflict_solution`
差：`info1`、`tech_stuff`、`user_said`

## 类别

- preference — 用户的偏好和习惯
- profile — 用户身份、角色、背景
- project — 项目信息、时间线、目标
- technical_decision — 技术选型和架构决定
- fact — 已确认的客观事实
- solution — 问题的解决方案
- unresolved — 未解决的问题或待办
- other — 其他

集合型属性使用稳定的复数 key，每个成员单独输出，例如 `liked_fruit: 苹果` 和 `liked_fruit: 梨` 可以同时成立。不要把 `liked_fruit` 改写成单值的 `favorite_fruit`；“最喜欢”才使用单值 key。只使用当前对话表达的语义，不因旧记忆 key 相似而合并。

## 置信度

- 1.0 — 用户明确陈述
- 0.8-0.9 — 强推定（如用户采纳了方案）
- 0.6-0.7 — 推断
- <0.6 — 丢弃，不输出

## 格式约束

最多 20 条；key 使用小写 snake_case、≤30 字符；value ≤ 500 字符；confidence 为 0.6 到 1.0。

输出纯 JSON（无 markdown 围栏）:
{{"atomic_facts": [{{"key": "...", "value": "...", "category": "...", "confidence": 0.8, "polarity": "affirmed", "claim_intent": "assert"}}]}}
撤回示例字段：`{{"key":"preferred_language","value":null,"retracts_value":"Rust","category":"preference","confidence":1.0,"polarity":"affirmed","claim_intent":"retract"}}`。`retracts_value` 必须明确指向用户当前撤回的值；不确定目标时不输出撤回候选。

## 示例

输入:
已有记忆: (无已有记忆)
对话: [用户] 我把新项目从 Python 换成了 Rust。[助手] 好选择！[用户] 目标 2026 Q3 发布。

输出: {{"atomic_facts": [{{"key": "preferred_language", "value": "Rust", "category": "preference", "confidence": 1.0, "claim_intent": "correct"}}, {{"key": "release_date", "value": "2026 Q3", "category": "project", "confidence": 1.0}}]}}

输入:
已有记忆: - preferred_language: Python (category: preference)
对话: [用户] 端口被占用了怎么办？[助手] 可以用 lsof -i :8080 找到占用进程后 kill。[用户] 用 lsof 那个方案吧。

输出: {{"atomic_facts": [{{"key": "port_conflict_solution", "value": "lsof -i :<port> 查找占用进程后 kill", "category": "solution", "confidence": 0.9}}]}}

输入:
已有记忆: - language: Rust (category: preference)
对话: [用户] Rust 编译太慢了，还是换回 Python 吧。[助手] 好的。

输出: {{"atomic_facts": [{{"key": "preferred_language", "value": "Python", "category": "preference", "confidence": 1.0, "claim_intent": "correct"}}]}}

---

已有记忆:
{existing_memories}

对话内容:
{conversation_text}
