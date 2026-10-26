判断一条候选与同范围、同主体、同事实键的已有记忆之间的关系，只返回一个 JSON 对象。

允许的 action：CREATE、REVISE、IGNORE、DEFER。
允许的 relation：supersedes、contradicts、null。

- 已有目标列表中的 ID 才能作为 target_memory_id；不可生成或改写 ID。
- CREATE + contradicts 表示保留新旧两条事实，并填写被矛盾的 target_memory_id。
- REVISE + supersedes 仅用于用户明确更正或撤回；必须填写目标 ID。
- IGNORE 仅用于语义等价的事实，并填写等价目标 ID。
- 撤回候选必须命中其 `retracts_value` 对应的现有值；不能只因 key 相同就撤回其他成员。
- 无法确认目标或用户是否在更正时选择 DEFER，不猜测。
- `set` 基数允许不同成员同时成立；只对同成员的相反 polarity 建立矛盾。`unknown` 基数不能凭 key 相同创建矛盾或覆盖。
- 单值冲突组已有多个不同 active 值时，选择 DEFER；不允许只修订其中一项并留下未处理的分叉。
- 不确定不等于 UPDATE；不能覆盖已有记忆。
- 只有 CREATE 且没有同键候选时，target_memory_id 和 relation 才可为 null。

已有记忆：
{existing_memory}

新候选：
{candidate}

输出格式：{{"action":"DEFER","target_memory_id":null,"relation":null,"reason_code":"uncertain_intent"}}
