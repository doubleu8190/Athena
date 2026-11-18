---
name: 月度薪资核算
slug: monthly-payroll
description: 根据薪资 MCP 聚合结果，按自然日在职天数和部门归属核算月度薪资并生成 Excel。
dependencies:
  - openpyxl>=3.1
scripts:
  - path: scripts/build_payroll.py
    label: 生成月度薪资 Excel
    purpose: 校验七类薪资数据，计算员工及部门薪资，生成 Excel，并单列缺数据员工。
    parameters:
      type: object
      additionalProperties: false
      properties:
        month:
          type: string
          title: 核算月份
          description: 必须与 payroll_inputs.month 一致，格式为 YYYY-MM。
          pattern: '^[0-9]{4}-(0[1-9]|1[0-2])$'
        department_id:
          type: string
          title: 核算部门编号
          description: 用户指定月份内需要核算的唯一部门 ID，例如 D500。
          minLength: 1
        payroll_inputs:
          type: object
          title: 薪资聚合数据
          description: 直接传入 payroll_query 工具返回的 structuredContent，不要手工改写记录。
          additionalProperties: false
          properties:
            schema_version:
              type: integer
              const: 1
            month:
              type: string
            month_days:
              type: integer
              minimum: 28
              maximum: 31
            daily_divisor:
              type: number
              const: 21.75
            employees:
              type: array
            department_assignments:
              type: array
            base_salaries:
              type: array
            unpaid_leaves:
              type: array
            bonuses:
              type: array
            annual_leave_balances:
              type: array
            terminations:
              type: array
          required:
            - schema_version
            - month
            - month_days
            - daily_divisor
            - employees
            - department_assignments
            - base_salaries
            - unpaid_leaves
            - bonuses
            - annual_leave_balances
            - terminations
      required:
        - month
        - department_id
        - payroll_inputs
---
本 Skill 用于已授权管理员的确定性薪资核算测试。

执行顺序：

1. 从用户请求中提取核算月份和唯一的 `department_id`，不要把其他部门合并到同一次 Skill 调用。
2. 先调用 MCP 工具 `payroll_query`，参数为需要核算的月份。
3. 将用户指定的 `department_id` 作为 Skill 顶层参数，并将工具返回的 `structuredContent` 原样作为
   `payroll_inputs` 传给 `scripts/build_payroll.py`，不要手工拼接、截断或改写七类记录。
4. 脚本先按该部门的任职记录确定员工范围，缺少月基本工资的员工也必须进入该部门的失败明细；
   其他部门员工、明细、奖金和失败记录不得进入报告。
5. 脚本按目标部门任职自然日分摊月基本工资，月自然日作为分摊分母；日薪固定使用月基本工资除以
   `21.75`。跨部门入离职只计算目标部门区间。
6. 无薪假按休假日期归属部门，并从日薪中扣款；奖金按记录中的部门归集。
7. 离职员工最后任职部门等于目标部门时，按日薪结算年假余额，正数计入、负数冲减；余额记录缺失时
   该员工失败。离职最终部门不是目标部门时，不在本次核算中结算年假。
8. 任一目标部门员工缺少月基本工资、完整部门任职区间、目标部门离职年假余额或其他必要记录时，
   该员工不进入任何薪资和总额，只写入目标部门的“失败明细”页。
9. 指定部门没有任何任职记录时，脚本以 `department_not_found` 明确失败，不得生成空报告。
10. 脚本会在 manifest 中发布 Excel 和 UTF-8 失败摘要。Skill 调用结果只提供交付文件引用，
   stdout 不会进入模型上下文，不能依赖 stdout 获取失败详情。
11. 从本次 Skill 调用返回的交付文件中找到标签为“薪资核算失败摘要”的文本文件，使用
   `session_file_read` 读取其 `file_id`。若返回 `truncated` 或 `eof=false`，按
   `next_offset` 继续读取，直到摘要完整。
12. 读取摘要后，向用户报告核算月份、部门 ID、Excel 产物、成功人数、失败人数，以及每名失败
    员工的编号、姓名和原因。

不要由模型重新计算金额。金额、日期覆盖、部门归属和失败隔离均以脚本结果为准。
