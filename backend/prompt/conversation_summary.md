从对话中提炼连贯的叙事摘要，供长期记忆检索使用。与原子事实提取（FactExtractor）互补：FactExtractor 提取离散的 key/value 点，你提取完整的问题解决过程和决策链。

## 提取标准

值得提取的内容（按优先级）：
1. **决策及理由** — 用户做了什么选择、为什么
2. **问题解决过程** — 遇到了什么问题、尝试了什么、最终怎么解决的
3. **技术讨论主线** — 有实质内容的技术方案讨论
4. **规划与约定** — 时间线、里程碑、待办事项

不提取：闲聊、一次性问答、客套话、通用技术解释（如"什么是 REST API"）、纯工具调用日志。

## 规则

- 每条摘要对应一个讨论主线或问题解决过程，保留关键上下文、决策与理由
- 工具输出仅提取关键结论（错误码、最终结果），丢弃原始日志和中间输出
- 讨论未完成 → category 标注 open_discussion，confidence ≤ 0.6
- 同一话题的多轮讨论合并为一条，而非拆成多条
- 最多 5 条；topic 为简短标签（≤20 字）；content 为 2-3 句话（≤300 字符）

## 输出格式

输出纯 JSON（无 markdown 围栏）:
{{"summaries": [{{"topic": "...", "content": "...", "category": "...", "confidence": 0.8}}]}}

类别取值:
- decision — 做出了明确决定
- problem_solving — 解决了具体问题
- technical_discussion — 技术方案讨论
- planning — 规划与安排
- open_discussion — 讨论未完成
- other — 其他

置信度: 明确陈述 0.9-1.0，强推定 0.7-0.8，推断 0.5-0.6（<0.5 丢弃）

## 示例

输入: [用户] 从 MySQL 换到 PostgreSQL，需要 JSONB。[助手] 合理，JSONB 对文档查询更友好。[用户] 下个 sprint 先跑试点。
输出: {{"summaries": [{{"topic": "数据库迁移", "content": "决定从 MySQL 迁移到 PostgreSQL，原因是需要 JSONB 支持。计划下个 sprint 先跑试点验证。", "category": "decision", "confidence": 0.9}}]}}

输入: [用户] 登录接口 500 了。[助手] 检查了日志，是 JWT 密钥过期。[用户] 换了新密钥，恢复了。
输出: {{"summaries": [{{"topic": "登录接口故障排查", "content": "登录接口返回 500，排查发现是 JWT 密钥过期导致。更换新密钥后恢复正常。", "category": "problem_solving", "confidence": 1.0}}]}}

对话内容:
{conversation}
