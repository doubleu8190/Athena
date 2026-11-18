"""提供固定薪资测试数据的 stdio MCP Server。"""

from __future__ import annotations

import asyncio
import calendar

from mcp.server import MCPServer

type PayrollRecord = dict[str, object]

SUPPORTED_MONTH = "2026-11"
DAILY_DIVISOR = 21.75

EMPLOYEES: tuple[PayrollRecord, ...] = (
    {"employee_id": "E001", "name": "张晨"},
    {"employee_id": "E002", "name": "李然"},
    {"employee_id": "E003", "name": "王宁"},
    {"employee_id": "E004", "name": "赵敏"},
    # 该员工故意缺少月基本工资，用于验证单人失败与总额排除。
    {"employee_id": "E005", "name": "陈舟"},
)

DEPARTMENT_ASSIGNMENTS: tuple[PayrollRecord, ...] = (
    {
        "employee_id": "E001",
        "department_id": "D100",
        "department_name": "研发部",
        "start_date": "2026-11-01",
        "end_date": None,
    },
    {
        "employee_id": "E002",
        "department_id": "D200",
        "department_name": "销售部",
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
    },
    {
        "employee_id": "E002",
        "department_id": "D300",
        "department_name": "客户成功部",
        "start_date": "2026-11-16",
        "end_date": "2026-11-30",
    },
    {
        "employee_id": "E003",
        "department_id": "D400",
        "department_name": "运营部",
        "start_date": "2026-11-01",
        "end_date": "2026-11-20",
    },
    {
        "employee_id": "E004",
        "department_id": "D500",
        "department_name": "财务部",
        "start_date": "2026-11-01",
        "end_date": "2026-11-12",
    },
    {
        "employee_id": "E005",
        "department_id": "D500",
        "department_name": "财务部",
        "start_date": "2026-11-01",
        "end_date": None,
    },
)

BASE_SALARIES: tuple[PayrollRecord, ...] = (
    {"employee_id": "E001", "monthly_base_salary": 21750},
    {"employee_id": "E002", "monthly_base_salary": 21750},
    {"employee_id": "E003", "monthly_base_salary": 17400},
    {"employee_id": "E004", "monthly_base_salary": 13050},
)

UNPAID_LEAVES: tuple[PayrollRecord, ...] = (
    {"employee_id": "E001", "leave_date": "2026-11-05", "days": 1},
    {"employee_id": "E002", "leave_date": "2026-11-20", "days": 1},
    {"employee_id": "E004", "leave_date": "2026-11-10", "days": 0.5},
)

BONUSES: tuple[PayrollRecord, ...] = (
    {"employee_id": "E001", "department_id": "D100", "amount": 2000},
    {"employee_id": "E002", "department_id": "D200", "amount": 600},
    {"employee_id": "E002", "department_id": "D300", "amount": 400},
    {"employee_id": "E003", "department_id": "D400", "amount": 500},
    {"employee_id": "E004", "department_id": "D500", "amount": 300},
    {"employee_id": "E005", "department_id": "D500", "amount": 100},
)

ANNUAL_LEAVE_BALANCES: tuple[PayrollRecord, ...] = (
    {"employee_id": "E003", "annual_leave_days": 5},
    {"employee_id": "E004", "annual_leave_days": -3},
)

TERMINATIONS: tuple[PayrollRecord, ...] = (
    {
        "employee_id": "E003",
        "termination_date": "2026-11-20",
        "termination_department_id": "D400",
        "termination_department_name": "运营部",
    },
    {
        "employee_id": "E004",
        "termination_date": "2026-11-12",
        "termination_department_id": "D500",
        "termination_department_name": "财务部",
    },
)

PAYROLL_DATA: tuple[tuple[str, tuple[PayrollRecord, ...]], ...] = (
    ("employees", EMPLOYEES),
    ("department_assignments", DEPARTMENT_ASSIGNMENTS),
    ("base_salaries", BASE_SALARIES),
    ("unpaid_leaves", UNPAID_LEAVES),
    ("bonuses", BONUSES),
    ("annual_leave_balances", ANNUAL_LEAVE_BALANCES),
    ("terminations", TERMINATIONS),
)

server = MCPServer("athena-payroll-test")


def _copy_items(values: tuple[PayrollRecord, ...]) -> list[PayrollRecord]:
    """复制固定记录，避免 MCP 调用方修改模块级测试数据。

    参数:
        values: 不可变元组中的可序列化记录。

    返回:
        逐条浅拷贝后的记录列表。

    异常:
        本函数不主动抛出业务异常。
    """
    return [dict(item) for item in values]


def _build_payroll_payload(month: str) -> dict[str, object]:
    """组装固定月份的完整薪资结构化内容。

    参数:
        month: 需要核算的月份，当前测试夹具仅支持 ``2026-11``。

    返回:
        包含月份元数据和全部七类固定记录的结构化 MCP 输出。

    异常:
        请求月份不是固定测试月份时抛出 ``ValueError``。
    """
    if month != SUPPORTED_MONTH:
        raise ValueError("payroll_fixture_month_unsupported")

    year_text, month_text = SUPPORTED_MONTH.split("-", maxsplit=1)
    month_days = calendar.monthrange(int(year_text), int(month_text))[1]
    payload: dict[str, object] = {
        "schema_version": 1,
        "month": SUPPORTED_MONTH,
        "month_days": month_days,
        "daily_divisor": DAILY_DIVISOR,
    }
    # 返回副本既保持原始 structuredContent 结构，也隔离每次调用的数据变更。
    payload.update((name, _copy_items(values)) for name, values in PAYROLL_DATA)
    return payload


@server.tool()
async def payroll_query(month: str) -> dict[str, object]:
    """返回固定月份的员工、部门、工资、无薪假、奖金、年假和离职数据。

    参数:
        month: 需要核算的 ``YYYY-MM`` 月份，当前仅支持 ``2026-11``。

    返回:
        可直接作为 Skill ``payroll_inputs`` 参数的结构化原始数据。

    异常:
        请求月份不是固定测试月份时抛出 ``ValueError``。
    """
    return _build_payroll_payload(month)


def main() -> None:
    """启动只暴露一个聚合查询工具的 stdio MCP Server。

    参数:
        无参数。

    返回:
        无返回值；运行至标准输入关闭。

    异常:
        MCP SDK 在协议启动失败时向上抛出异常。
    """
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
