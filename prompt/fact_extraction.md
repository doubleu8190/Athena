从对话中提取离散的原子事实，供长期记忆检索使用。与对话摘要（ConversationSummarizer）互补：你提取独立的 key/value 信息点，它提取完整的问题解决过程和决策链。

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
- 新信息更新已有记忆 → value 中标注旧值，如 "Rust (was Python)"

## 去重

已有记忆中语义等价的不重复提取。若新信息是对已有记忆的更新或修正，提取并在 value 中标注变化。

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

## 置信度

- 1.0 — 用户明确陈述
- 0.8-0.9 — 强推定（如用户采纳了方案）
- 0.6-0.7 — 推断
- <0.6 — 丢弃，不输出

## 格式约束

最多 20 条；key ≤ 30 字符；value ≤ 100 字符。

输出纯 JSON（无 markdown 围栏）:
{{"atomic_facts": [{{"key": "...", "value": "...", "category": "...", "confidence": 0.8}}]}}

## 示例

输入:
已有记忆: (无已有记忆)
对话: [用户] 我把新项目从 Python 换成了 Rust。[助手] 好选择！[用户] 目标 2026 Q3 发布。

输出: {{"atomic_facts": [{{"key": "preferred_language", "value": "Rust (was Python)", "category": "preference", "confidence": 1.0}}, {{"key": "release_date", "value": "2026 Q3", "category": "project", "confidence": 1.0}}]}}

输入:
已有记忆: - preferred_language: Python (category: preference)
对话: [用户] 端口被占用了怎么办？[助手] 可以用 lsof -i :8080 找到占用进程后 kill。[用户] 用 lsof 那个方案吧。

输出: {{"atomic_facts": [{{"key": "port_conflict_solution", "value": "lsof -i :<port> 查找占用进程后 kill", "category": "solution", "confidence": 0.9}}]}}

输入:
已有记忆: - language: Rust (category: preference)
对话: [用户] Rust 编译太慢了，还是换回 Python 吧。[助手] 好的。

输出: {{"atomic_facts": [{{"key": "preferred_language", "value": "Python (was Rust)", "category": "preference", "confidence": 1.0}}]}}

---

已有记忆:
{existing_memories}

对话内容:
{conversation_text}
