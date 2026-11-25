# 星辰智联 HR/OA 模拟 stdio MCP Server

本目录提供 Athena v2 企业场景测试数据体系（V1 + V2 + V3）的统一 HR/OA 模拟 stdio MCP Server，覆盖「星辰智联科技有限公司（支持 `2026-11` 与 `2026-12` 双月度演进视图）」14 人统一花名册、假期余额、OA 在办工单、离职四项清查状态、2026 年度绩效校准、合同用印审批、IT 工单与 2026-11 / 2026-12 共 12 笔差旅报销稽核（非法月份返回 `month_unsupported`）。

## 三 Profile 设计（3 实例 16 工具）

| Profile | 建议注册名称 | 权限层级 | 暴露工具（共 16 个） |
| --- | --- | --- | --- |
| `standard` | `hroa-standard` | 公司级 (`company`) | `hroa_get_employee_profile`、`hroa_get_leave_balance`、`hroa_list_oa_tickets`、`hroa_check_offboarding_clearance`、`hroa_submit_leave_request`、`hroa_submit_transfer_request`、`hroa_get_performance_review`、`hroa_submit_contract_seal_request`、`hroa_submit_it_asset_request` |
| `hr_ops` | `hroa-hr-ops` | 部门级 (`dept_mock_hr`) | `hroa_list_department_roster`、`hroa_initiate_onboarding`、`hroa_initiate_offboarding`、`hroa_update_clearance_item`、`hroa_submit_performance_calibration` |
| `fin_ops` | `hroa-fin-ops` | 部门级 (`dept_mock_fin`) | `hroa_list_travel_expenses`、`hroa_flag_expense_audit_result` |

## 并发安全与状态复位

- 三个 Profile 通过 `--state-file`（或环境变量 `HROA_STATE_PATH`）共享同一份 JSON 状态叠加文件。
- 每次读写均使用同级 `.lock` 文件加 `fcntl.flock` 文件锁，并通过临时文件 + `os.replace` 原子写回，保障 API / Worker 跨进程一致。
- 执行以下命令可一键复位至 `2026-11-24` 基线：

```sh
python mcp_servers/hroa/mcp_server.py --state-file /tmp/athena-hroa-test/state.json --reset-state
```

## 容器内注册示例

```json
{
  "name": "hroa-standard",
  "transport": "stdio",
  "command": "/usr/local/bin/python",
  "args": [
    "/app/mcp_servers/hroa/mcp_server.py",
    "--profile",
    "standard",
    "--state-file",
    "/app/var/hroa-test/state.json"
  ],
  "cwd": "/app/mcp_servers/hroa",
  "secrets": {}
}
```
