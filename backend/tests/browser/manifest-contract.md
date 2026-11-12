# Browser E2E manifest contract

Harness 启动后写入：

/private/tmp/athena-skills-browser-<id>/state.json

同一路径导出到环境变量 ATHENA_E2E_STATE_FILE，文件权限为 0600。前端 live config
应从该文件读取，不应硬编码账号、路径或端口。

## 顶层字段

| 字段 | 含义 |
| --- | --- |
| contract_version | 当前为 1 |
| state_file | manifest 绝对路径 |
| frontend_base_url | 固定为 http://127.0.0.1:4183 |
| api_base_url | 通常为 http://127.0.0.1:8011，端口冲突时可能变化 |
| health_url | API 健康检查 URL |
| accounts.admin | email、password、display_name，通过 UI 正常登录 |
| accounts.ordinary | 普通用户真实账号 |
| skill | v1 ZIP、v2 ZIP、slug、名称、脚本路径和 guide marker |
| input | 上传 CSV 路径、文件名和校验文本 |
| provider | scripted 或 configured-default，以及是否验证了真实外部 LLM |
| scenarios | 固定提示词、审批决策、预期终态和产物名 |
| resources | schema、随机 MinIO bucket、Redis 容器和临时执行根 |
| stop_request_path | 创建该文件后 harness 停止并清理资源 |

## 场景键

- normal：审批通过，预期 e2e-report.xlsx、e2e-trend.png 和 e2e-summary.txt。
- reuse：选择 e2e-summary.txt 发起后续 Run，provider 只调用 session_file_read 并总结，
  不重新加载或执行 Skill；保持 Skill enabled 也能验证用户本轮未选择 Skill。
- approval_reject：审批拒绝，Run 正常结束但不产生交付文件。
- cancel_slow：审批通过后执行 120 秒慢脚本，浏览器随后取消。
- version_update_pin：旧 Run 待审批时导入 v2，旧 Run 仍执行 v1。
- disable_pin：旧 Run 待审批时停用 Skill，旧 Run 仍执行 v1。

每次运行使用独立 schema、独立随机 MinIO bucket 和独立 Redis 容器。GC 只会访问该
bucket，不会读取共享旧文件桶。浏览器测试不得调用 route.fulfill 替代 API；应由
Vite live config 将 /api 代理到 api_base_url。

cancel_slow 的脚本在进入 120 秒等待前写入本调用工作目录的
`.e2e-script-started` 标记。浏览器可等待
`resources.skill_temp_root/skill-<tool_call_id 前 32 字符>-*/.e2e-script-started`
出现后再取消；该标记不是 manifest 输出，也不会成为用户文件。

默认 provider.mode=scripted 使用本机真实 HTTP/SSE ChatCompletions provider，
覆盖真实协议、Adapter、Worker 和工具链，但不能宣称验证了真实外部 LLM。

`--model-source configured-default` 只读克隆 dev 默认模型配置到隔离 schema，并压低
输出、请求和执行时限；温度与 `supports_*` 能力声明保持原值，不打印或修改模型名、
URL、密钥或原记录。
