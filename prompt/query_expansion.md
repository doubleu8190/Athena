将用户查询改写为适合向量语义检索的搜索语句。检索目标是用户的长期记忆库，其中存储的是两类结构化内容：
- 原子事实（key: value 格式，如 "preferred_language: Rust"、"release_date: 2026 Q3"）
- 对话摘要（topic: content 格式，如 "数据库迁移: 决定从 MySQL 迁移到 PostgreSQL"）

## 改写规则

1. **保留核心实体** — 人名、项目名、技术名词、日期、数字等必须原样保留，不要泛化
2. **聚焦意图** — 明确用户在找什么，去掉闲聊成分和冗余修饰
3. **适度扩展** — 补充1-2个语义相关的关键词，但不要过度发散。记忆库条目通常简短精确，过度扩展会召回弱相关结果
4. **保持语言** — 用与原始查询相同的语言输出
5. **一行输出** — 只返回改写后的搜索语句，无解释、无前缀、无引号、无编号

## 示例

用户: "我之前用的什么数据库来着"
→ 数据库选型 技术栈 database

用户: "Rust 项目什么时候发布"
→ Rust 项目发布日期 release_date

用户: "上次那个端口冲突怎么解决的"
→ 端口冲突 解决方案 port conflict solution

用户: "What framework did I choose for the frontend?"
→ frontend framework choice UI library

---

原始查询：{query}
