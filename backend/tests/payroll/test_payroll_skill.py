"""薪资内置数据、stdio MCP 与 Athena Skill 的真实闭环测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from mcp import Client
from openpyxl import load_workbook  # type: ignore[import-untyped]

from adapter.skill.execution import PythonScriptExecutor
from app.skill.package import SkillPackageValidator
from domain.skill.execution import JsonValue, ScriptExecutionRequest
from infrastructure.mcp.stdio import McpStdioTransport
from mcp_servers.payroll import mcp_server as payroll_server
from payroll.skill_package.scripts.build_payroll import PayrollInputError, build_payroll_report

TEST_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = TEST_DIRECTORY.parents[2]
MCP_SERVER_DIRECTORY = REPOSITORY_ROOT / "mcp_servers" / "payroll"
MCP_SERVER_PATH = MCP_SERVER_DIRECTORY / "mcp_server.py"
SKILL_SOURCE_DIRECTORY = TEST_DIRECTORY / "skill_package"


def _build_skill_zip(destination: Path) -> Path:
    """把 Athena Skill 源目录打成可由管理接口导入的 ZIP。

    参数:
        destination: 临时 ZIP 文件路径。

    返回:
        已写入的 ZIP 路径。

    异常:
        源目录或 ZIP 无法读写时传播底层 ``OSError``。
    """
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source_path in sorted(SKILL_SOURCE_DIRECTORY.rglob("*")):
            if not source_path.is_file():
                continue
            relative_path = source_path.relative_to(SKILL_SOURCE_DIRECTORY)
            if "__pycache__" in relative_path.parts or source_path.suffix in {".pyc", ".pyo"}:
                continue
            archive.write(source_path, relative_path.as_posix())
    return destination


def _extract_package(package_path: Path, working_directory: Path) -> None:
    """把已校验的 Skill ZIP 展开到测试执行目录。

    参数:
        package_path: 已通过 Athena 校验器的 ZIP。
        working_directory: Python 执行器工作目录。

    返回:
        无返回值。

    异常:
        ZIP 无法读取或目标目录不可写时传播底层异常。
    """
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(working_directory)
    (working_directory / "outputs").mkdir()


def _sheet_rows(workbook_path: Path, sheet_name: str) -> list[dict[str, object]]:
    """把一张 Excel 工作表读取为以表头为键的行。

    参数:
        workbook_path: 待读取 Excel 路径。
        sheet_name: 工作表名称。

    返回:
        不包含表头的逐行字典。

    异常:
        工作簿或工作表不存在时传播 openpyxl 异常。
    """
    workbook = load_workbook(workbook_path, data_only=True, read_only=True)
    try:
        sheet = workbook[sheet_name]
        rows = sheet.iter_rows(values_only=True)
        headers = tuple(str(value) for value in next(rows))
        return [{header: value for header, value in zip(headers, row, strict=True)} for row in rows]
    finally:
        workbook.close()


def _header_number_formats(workbook_path: Path, sheet_name: str) -> dict[str, str]:
    """读取工作表首行数据各列的 Excel 数字格式。

    参数:
        workbook_path: 待读取 Excel 路径。
        sheet_name: 工作表名称。

    返回:
        以表头为键、首行数据数字格式为值的字典。

    异常:
        工作簿、工作表或首行数据不存在时传播 openpyxl 异常。
    """
    workbook = load_workbook(workbook_path, read_only=False)
    try:
        sheet = workbook[sheet_name]
        headers = tuple(str(cell.value) for cell in sheet[1])
        return {
            header: sheet.cell(row=2, column=index).number_format
            for index, header in enumerate(headers, start=1)
        }
    finally:
        workbook.close()


def _duplicate_department_payload() -> dict[str, object]:
    """构造同一员工当月 D100→D200→D100 的核算输入。

    参数:
        无参数。

    返回:
        可直接交给 ``build_payroll_report`` 的完整七类数据。

    异常:
        本函数不主动抛出业务异常。
    """
    return {
        "schema_version": 1,
        "month": "2026-11",
        "month_days": 30,
        "daily_divisor": 21.75,
        "employees": [{"employee_id": "E100", "name": "测试员工"}],
        "department_assignments": [
            {
                "employee_id": "E100",
                "department_id": "D100",
                "department_name": "研发部",
                "start_date": "2026-11-01",
                "end_date": "2026-11-10",
            },
            {
                "employee_id": "E100",
                "department_id": "D200",
                "department_name": "销售部",
                "start_date": "2026-11-11",
                "end_date": "2026-11-20",
            },
            {
                "employee_id": "E100",
                "department_id": "D100",
                "department_name": "研发部",
                "start_date": "2026-11-21",
                "end_date": "2026-11-30",
            },
        ],
        "base_salaries": [{"employee_id": "E100", "monthly_base_salary": 21750}],
        "unpaid_leaves": [
            {"employee_id": "E100", "leave_date": "2026-11-05", "days": 1},
            {"employee_id": "E100", "leave_date": "2026-11-25", "days": 2},
        ],
        "bonuses": [
            {"employee_id": "E100", "department_id": "D100", "amount": 2000},
            {"employee_id": "E100", "department_id": "D200", "amount": 600},
        ],
        "annual_leave_balances": [{"employee_id": "E100", "annual_leave_days": 5}],
        "terminations": [
            {
                "employee_id": "E100",
                "termination_date": "2026-11-30",
                "termination_department_id": "D100",
            }
        ],
    }


def _builtin_payroll_payload() -> dict[str, object]:
    """把 MCP Server 内置记录组装成 Skill 可直接接收的七类输入。

    参数:
        无参数。

    返回:
        固定月份、元数据和七类内置记录的副本。

    异常:
        本函数不主动抛出业务异常。
    """
    return {
        "schema_version": 1,
        "month": payroll_server.SUPPORTED_MONTH,
        "month_days": 30,
        "daily_divisor": 21.75,
        "employees": [dict(item) for item in payroll_server.EMPLOYEES],
        "department_assignments": [dict(item) for item in payroll_server.DEPARTMENT_ASSIGNMENTS],
        "base_salaries": [dict(item) for item in payroll_server.BASE_SALARIES],
        "unpaid_leaves": [dict(item) for item in payroll_server.UNPAID_LEAVES],
        "bonuses": [dict(item) for item in payroll_server.BONUSES],
        "annual_leave_balances": [dict(item) for item in payroll_server.ANNUAL_LEAVE_BALANCES],
        "terminations": [dict(item) for item in payroll_server.TERMINATIONS],
    }


def test_payroll_stdio_mcp_skill_and_excel_end_to_end(tmp_path: Path) -> None:
    """七类内置数据经真实 stdio MCP 进入合法 Skill 并生成可核对 Excel。"""
    package_path = _build_skill_zip(tmp_path / "monthly-payroll.zip")
    validated = SkillPackageValidator().validate(package_path)
    assert validated.manifest.slug == "monthly-payroll"
    assert validated.manifest.dependencies == ("openpyxl>=3.1",)
    assert validated.manifest.scripts[0].path == "scripts/build_payroll.py"
    assert validated.manifest.scripts[0].parameters["required"] == [
        "month",
        "department_id",
        "payroll_inputs",
    ]

    observed_employee_rows: list[dict[str, object]] = []
    observed_department_rows: list[dict[str, object]] = []
    observed_failure_rows: list[dict[str, object]] = []
    observed_detail_rows: list[dict[str, object]] = []

    async def scenario() -> None:
        transport = McpStdioTransport(
            sys.executable,
            (str(MCP_SERVER_PATH),),
            cwd=str(MCP_SERVER_DIRECTORY),
            base_environment={"PATH": os.defpath},
        )
        async with Client(
            transport,
            mode="auto",
            read_timeout_seconds=10,
            cache=None,
        ) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ["payroll_query"]
            result = await client.call_tool(
                "payroll_query",
                {"month": payroll_server.SUPPORTED_MONTH},
            )
            assert result.is_error is False
            payroll_inputs = cast(dict[str, object], result.structured_content)

        assert payroll_inputs["month"] == payroll_server.SUPPORTED_MONTH
        assert payroll_inputs["month_days"] == 30
        assert payroll_inputs["employees"] == [dict(item) for item in payroll_server.EMPLOYEES]

        working_directory = tmp_path / "skill-execution"
        working_directory.mkdir()
        _extract_package(package_path, working_directory)
        request = ScriptExecutionRequest(
            working_directory=working_directory,
            script_path="scripts/build_payroll.py",
            parameters={
                "month": payroll_server.SUPPORTED_MONTH,
                "department_id": "D500",
                "payroll_inputs": cast(JsonValue, payroll_inputs),
            },
            input_files={},
            output_directory=working_directory / "outputs",
            run_id="run_payroll_e2e",
            tool_call_id="toolcall_payroll_e2e",
            worker_instance_id="worker_payroll_e2e",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(request) as execution:
            assert execution.succeeded, execution.stderr.decode(errors="replace")
            outputs = tuple(execution.outputs)
            assert [(item.path, item.label) for item in outputs] == [
                (
                    "payroll-2026-11.xlsx",
                    "2026-11 D500 月度薪资核算",
                ),
                (
                    "payroll-2026-11-failures.txt",
                    "2026-11 D500 薪资核算失败摘要",
                ),
            ]
            workbook_path = outputs[0].source_path
            failure_summary = outputs[1].source_path.read_text(encoding="utf-8")
            assert "核算部门：D500" in failure_summary
            assert "E005 陈舟：缺少月基本工资" in failure_summary
            stdout_payload = json.loads(execution.stdout)
            assert stdout_payload["department_id"] == "D500"
            assert stdout_payload["failed_employee_details"] == [
                {
                    "employee_id": "E005",
                    "employee_name": "陈舟",
                    "reason": "缺少月基本工资",
                }
            ]
            observed_employee_rows.extend(_sheet_rows(workbook_path, "员工汇总"))
            observed_department_rows.extend(_sheet_rows(workbook_path, "部门汇总"))
            observed_detail_rows.extend(_sheet_rows(workbook_path, "核算明细"))
            observed_failure_rows.extend(_sheet_rows(workbook_path, "失败明细"))

            detail_number_formats = _header_number_formats(workbook_path, "核算明细")
            assert detail_number_formats["在职自然日"] == "0"
            assert detail_number_formats["月自然日"] == "0"
            assert detail_number_formats["无薪假天数"] == "0.00"
            assert detail_number_formats["年假天数"] == "0.00"
            assert detail_number_formats["月基本工资"] == "¥#,##0.00"

    asyncio.run(scenario())

    employee_totals = {
        str(row["员工编号"]): row["应付薪资"]
        for row in observed_employee_rows
        if row["员工编号"] != "合计"
    }
    assert employee_totals == {"E004": 3420}
    assert observed_employee_rows[-1]["员工编号"] == "合计"
    assert observed_employee_rows[-1]["应付薪资"] == 3420

    department_totals = {
        str(row["部门编号"]): row["部门应付薪资"]
        for row in observed_department_rows
        if row["部门编号"] != "合计"
    }
    assert department_totals == {"D500": 3420}
    assert observed_department_rows[-1]["部门编号"] == "合计"
    assert observed_department_rows[-1]["部门应付薪资"] == 3420

    detail_by_employee_department = {
        (str(row["员工编号"]), str(row["部门编号"])): row for row in observed_detail_rows
    }
    assert set(detail_by_employee_department) == {("E004", "D500")}
    assert detail_by_employee_department[("E004", "D500")]["在职自然日"] == 12
    assert detail_by_employee_department[("E004", "D500")]["年假结算"] == -1800
    assert detail_by_employee_department[("E004", "D500")]["无薪假扣款"] == 300

    assert len(observed_failure_rows) == 1
    assert observed_failure_rows[0]["员工编号"] == "E005"
    assert "缺少月基本工资" in str(observed_failure_rows[0]["失败原因"])
    assert all(row["员工编号"] != "E005" for row in observed_detail_rows)
    assert all(row["部门编号"] == "D500" for row in observed_detail_rows)


@pytest.mark.parametrize(
    ("department_id", "expected_employee_totals", "expected_total", "failed_employee_ids"),
    [
        ("D100", {"E001": Decimal("22750.00")}, Decimal("22750.00"), ()),
        ("D200", {"E002": Decimal("11475.00")}, Decimal("11475.00"), ()),
        ("D300", {"E002": Decimal("10275.00")}, Decimal("10275.00"), ()),
        ("D400", {"E003": Decimal("16100.00")}, Decimal("16100.00"), ()),
        ("D500", {"E004": Decimal("3420.00")}, Decimal("3420.00"), ("E005",)),
    ],
)
def test_each_department_is_calculated_without_cross_department_leakage(
    department_id: str,
    expected_employee_totals: dict[str, Decimal],
    expected_total: Decimal,
    failed_employee_ids: tuple[str, ...],
) -> None:
    """每个部门只返回本部门员工、明细、汇总和目标部门失败记录。"""
    report = build_payroll_report(_builtin_payroll_payload(), department_id)

    assert report.department_id == department_id
    assert {line.department_id for line in report.lines} == {department_id}
    assert {line.employee_id: line.payable_salary for line in report.lines} == (
        expected_employee_totals
    )
    assert {summary.department_id for summary in report.department_summaries} == {department_id}
    assert report.department_summaries[0].payable_salary == expected_total
    assert tuple(failure.employee_id for failure in report.failed_employees) == failed_employee_ids
    assert report.total_payable_salary == expected_total


def test_unknown_department_fails_explicitly() -> None:
    """目标部门没有任何任职记录时返回明确错误。"""
    with pytest.raises(PayrollInputError, match="department_not_found"):
        build_payroll_report(_builtin_payroll_payload(), "D999")


def test_repeated_department_uses_interval_leaves_and_department_level_amounts_once() -> None:
    """重复部门时按区间归属休假，奖金和离职年假结算每部门只计一次。"""
    report = build_payroll_report(_duplicate_department_payload(), "D100")

    assert report.failed_employees == ()
    assert [line.department_id for line in report.lines] == ["D100", "D100"]
    assert [line.employed_days for line in report.lines] == [10, 10]
    assert [line.unpaid_leave_days for line in report.lines] == [
        Decimal("1"),
        Decimal("2"),
    ]
    assert [line.unpaid_leave_deduction for line in report.lines] == [
        Decimal("1000.00"),
        Decimal("2000.00"),
    ]
    assert [line.bonus for line in report.lines] == [
        Decimal("2000.00"),
        Decimal("0.00"),
    ]
    assert [line.annual_leave_settlement for line in report.lines] == [
        Decimal("0.00"),
        Decimal("5000.00"),
    ]
    assert [line.payable_salary for line in report.lines] == [
        Decimal("8250.00"),
        Decimal("10250.00"),
    ]
    summaries = {summary.department_id: summary for summary in report.department_summaries}
    assert summaries["D100"].bonus == Decimal("2000.00")
    assert summaries["D100"].annual_leave_settlement == Decimal("5000.00")
    assert summaries["D100"].payable_salary == Decimal("18500.00")
    assert set(summaries) == {"D100"}
    assert report.total_payable_salary == Decimal("18500.00")


def test_payroll_skill_script_compiles() -> None:
    """真实 Skill 入口脚本必须通过 Python 编译检查。"""
    source = (SKILL_SOURCE_DIRECTORY / "scripts" / "build_payroll.py").read_text(encoding="utf-8")
    compile(source, "build_payroll.py", "exec")
    assert "21.75" in source
    assert "department_id" in source
    assert "annual_leave_settlement" in source
