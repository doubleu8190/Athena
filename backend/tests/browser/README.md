# Skills live browser harness

启动隔离后端：

source scripts/load-test-env.sh
PYTHONPATH=backend/src:backend/tests .venv/bin/python -m browser.harness

前端 live Vite 服务由前端 agent 的 frontend/e2e/skills.live.vite.config.ts
启动在 4183，并代理到 manifest 中的 api_base_url。Harness 不占用 4183。

默认使用本机 HTTP/SSE scripted ChatCompletions provider，API 尝试监听 8011，
provider 尝试监听 8012；实际端口以 state.json 为准。完整字段说明见
manifest-contract.md。

可选真实模型低调用验收：

source scripts/load-test-env.sh
PYTHONPATH=backend/src:backend/tests .venv/bin/python -m browser.harness \
  --model-source configured-default

停止时创建 manifest 的 stop_request_path 或向 harness 发送 SIGTERM。
SIGINT/SIGTERM 都进入同一个 finally 清理路径，并在 `cleanup.json` 记录终止信号。

`--model-source configured-default` 只读克隆 dev 默认模型配置到隔离 schema，并压低
输出、请求和执行时限；温度与 `supports_*` 能力声明保持原值，避免改变供应商兼容性。
不打印或修改模型名、URL、密钥或原记录。

`cleanup.json` 只在 schema 删除语句成功执行且复查确认不存在后写
`schema_dropped=true`；清理失败会保留脱敏错误类别并标记 `status=incomplete`。
每个已实际创建的子进程在 `process_cleanup` 中单独记录 role、pid、停止结果和退出码；
`processes_expected` 只统计真实创建数量，不包含未启动槽位。

取消场景先观察本调用工作目录中的 `.e2e-script-started`，再通过 UI 取消。该文件只用于
确认慢脚本已启动，不进入产物清单。

