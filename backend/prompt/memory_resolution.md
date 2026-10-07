判断新记忆候选与已有记忆的关系，只返回 JSON。

action 只能是 UPDATE、SUPERSEDE、IGNORE、CREATE；relation 只能是
supersedes、contradicts、supports 或 null。

矛盾但无法确认新值替代旧值时使用 CREATE + contradicts；独立证据支持旧记忆时使用
CREATE + supports；不确定时返回 UPDATE。

已有记忆：{existing_memory}
新候选：{candidate}

输出格式：{{"action":"UPDATE","relation":null}}
