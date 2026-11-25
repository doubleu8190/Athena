---
name: 入转调离月度风控稽核台账包
slug: hroa-lifecycle-audit-pack
description: 根据 HR/OA MCP 返回的部门月度花名册、假期余额、在办工单与离职清查数据，确定性生成四表稽核 Excel 与 UTF-8 风险摘要文本。
dependencies:
  - openpyxl>=3.1
scripts:
  - path: scripts/build_hroa_audit.py
    label: 生成入转调离月度风控稽核 Excel 与摘要
    purpose: 校验 HR/OA 聚合输入，按部门或全公司输出四张稽核工作表及阻断预警摘要文本。
    parameters:
      type: object
      additionalProperties: false
      properties:
        month:
          type: string
          title: 稽核月份
          description: 必须与 hroa_inputs.month 一致，格式为 YYYY-MM。
          pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$'
        department_id:
          type: string
          title: 目标部门编号
          description: 目标部门 ID（如 dept_mock_platform）或全公司范围 ALL。
          minLength: 1
        hroa_inputs:
          type: object
          title: HR/OA 聚合原始数据
          description: 直接传入 hroa_list_department_roster 工具返回的 structuredContent，不要手工改写。
          additionalProperties: false
          properties:
            schema_version:
              type: integer
              const: 1
            company_name:
              type: string
            baseline_date:
              type: string
            month:
              type: string
            department_id:
              type: string
            departments:
              type: array
            employees:
              type: array
            leave_balances:
              type: array
            oa_tickets:
              type: array
            offboarding_clearances:
              type: array
            year_end_carryover_rule:
              type: object
          required:
            - schema_version
            - month
            - departments
            - employees
            - leave_balances
            - oa_tickets
            - offboarding_clearances
      required:
        - month
        - department_id
        - hroa_inputs
---
本 Skill 用于星辰智联人力资源部（`dept_mock_hr`）执行月度全生命周期（入职、转正、调岗晋升、假勤、离职清查及跨年结转）确定性风控稽核。

执行顺序：

1. 从用户请求中提取目标稽核月份（`YYYY-MM`，例如 `2026-11` 或 `2026-12`）和目标 `department_id`（单个部门 ID 或 `"ALL"`）。
2. 先调用 HR 专属 MCP 工具 `hroa_list_department_roster`，传入对应 `department_id` 与 `month`。
3. 将 `month`、`department_id` 以及工具返回的 `structuredContent` 原样作为 `hroa_inputs` 传入 `scripts/build_hroa_audit.py`，不要手工改写、删减或重算任何字段。
4. 该脚本受人工审批门禁保护（`script_requires_approval=true`）；待确认执行后，脚本会在输出清单中发布两份交付文件：
   - 四工作表 Excel 稽核台账：`hroa-audit-<YYYY-MM>.xlsx`（含 `汇总总览`、`入职与试用期跟踪`、`离职清查与阻断项`、`异动与假勤预警`）；
   - UTF-8 风险摘要文本：`hroa-audit-<YYYY-MM>-summary.txt`。
5. 指定 `department_id` 不是 `"ALL"` 且在当前花名册中无匹配部门记录时，脚本会以 `department_not_found` 明确失败，不得生成空报告。
6. Skill 执行结果的 stdout 不进入模型上下文，请从交付文件中找到风险摘要文本文件，使用 `session_file_read` 完整读取（若返回 `truncated` 或 `eof=false`，按 `next_offset` 续读直至完整）。
7. 结合摘要向用户汇报稽核范围、各状态人数，并逐一列出所有阻断与预警员工的工号、姓名及处置建议（参考 `references/hroa-audit-rules.md`）。
