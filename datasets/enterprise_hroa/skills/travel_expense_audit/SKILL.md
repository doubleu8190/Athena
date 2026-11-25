---
name: 差旅报销超标与备用金稽核审计包
slug: travel-expense-audit-pack
description: 根据财务专属 MCP 返回的差旅报销明细、城市限额标准与备用金状态，按 XC-FIN-2026-006 与 XC-FIN-TRV-021 确定性生成四表稽核 Excel 与 UTF-8 风险摘要文本。
dependencies:
  - openpyxl>=3.1
scripts:
  - path: scripts/audit_travel_expenses.py
    label: 生成差旅超标与备用金稽核 Excel 及风险摘要
    purpose: 校验财务差旅报销聚合输入，逐笔核验住宿超标、餐补超标、舱位违规及离职/超期备用金阻断并输出四表工作簿与摘要。
    parameters:
      type: object
      additionalProperties: false
      properties:
        month:
          type: string
          title: 稽核月份
          description: 必须与 travel_inputs.month 一致，格式为 YYYY-MM。
          pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$'
        department_id:
          type: string
          title: 目标部门编号
          description: 目标部门 ID（如 dept_mock_platform）或全公司范围 ALL。
          minLength: 1
        travel_inputs:
          type: object
          title: 财务差旅报销聚合数据
          description: 直接传入 hroa_list_travel_expenses 工具返回的 structuredContent，不要手工改写或篡改金额。
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
            travel_standards:
              type: object
            expenses:
              type: array
            department_summary:
              type: array
            related_clearances:
              type: array
          required:
            - schema_version
            - month
            - departments
            - travel_standards
            - expenses
      required:
        - month
        - department_id
        - travel_inputs
---
本 Skill 用于星辰智联财务部（`dept_mock_fin`）执行月度差旅报销超标稽核、舱位合规审查及备用金逾期/离职阻断预警。

执行顺序：

1. 从用户请求中提取目标稽核月份（`YYYY-MM`，例如 `2026-11` 或 `2026-12`）和目标 `department_id`（单个部门 ID 或 `"ALL"`）。
2. 先调用财务专属 MCP 工具 `hroa_list_travel_expenses`，传入对应 `month` 与 `department_id`。
3. 将 `month`、`department_id` 以及工具返回的 `structuredContent` 原样作为 `travel_inputs` 传入 `scripts/audit_travel_expenses.py`，不要手工重算或删减字段。
4. 该脚本受人工审批门禁保护（`script_requires_approval=true`）；审批通过执行后，脚本会在输出清单中发布两份交付文件：
   - 四工作表 Excel 稽核台账：`travel-audit-<YYYY-MM>.xlsx`（含 `审计总览`、`超标与违规明细`、`合规放行清单`、`备用金逾期预警`）；
   - UTF-8 风险摘要文本：`travel-audit-<YYYY-MM>-summary.txt`。
5. 指定 `department_id` 不是 `"ALL"` 且无对应报销记录或部门不存在时，脚本将以 `department_not_found` 明确报错失败，绝不产出空报告。
6. Skill 执行的 stdout 不进入模型上下文，请使用 `session_file_read` 完整读取风险摘要文本文件（如返回 `eof=false`，按 `next_offset` 续读直至完整）。
7. 结合摘要与 `references/travel-audit-rules.md` 向财务审核人逐笔汇报超标金额、舱位违规及备用金阻断预警；若需落盘稽核结论，进一步调用 `hroa_flag_expense_audit_result`（触发中文审批卡片）。
