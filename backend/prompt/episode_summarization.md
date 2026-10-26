# 任务回顾提炼

根据一轮真实执行记录，判断是否值得保存一条可复用的任务回顾。只使用以下输入，不补造尝试、结果或验证过程。

## 输入

用户目标：
{user_request}

助手最终答复：
{assistant_response}

持久化执行证据：
{execution_evidence}

## 边界

- `retain=false` 用于简单问答、没有可复用过程或证据不足以形成回顾的任务。此时 `summary` 设为 null、`source_ref_ids` 设为空数组。
- `retain=true` 时记录目标、真实结果、重要尝试、可复用经验和遗留问题；无对应内容时使用空字符串或空数组。
- 只根据任务最终状态描述任务是否完成。工具状态 `succeeded` 只说明该次工具调用成功，不等于业务目标或端到端流程已验证。
- `verification_scope` 只填写证据明确证明过的范围；工具回执只有状态和引用时，留空，不要推测测试范围。
- `reusable_learnings` 只写能从本次目标、尝试、结果或来源证据中复用的做法；将其写成建议，不要声称已经验证或适用于所有环境。
- 失败、取消、部分完成和未解决问题都要如实保留。助手答复中的承诺不等于执行结果。
- `source_ref_ids` 只能选择 `execution_evidence.source_refs` 中已有的 `source_id`。不要生成 ID，也不要引用未提供的来源。
- 不要把原始密钥、访问令牌、个人敏感信息或工具参数写入回顾。

## 输出字段

返回 `retain`、`reason_code`、`summary`、`source_ref_ids`。`summary` 包含 `title`、`goal`、`outcome`、`result`、`attempts`、`verification_scope`、`reusable_learnings`、`unresolved`。`outcome` 只能为 `completed`、`partial`、`failed`、`cancelled`、`unknown`。
