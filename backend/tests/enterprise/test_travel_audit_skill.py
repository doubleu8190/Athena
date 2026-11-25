"""SK-7（travel-expense-audit-pack）ZIP 包结构与真实 fin_ops MCP 闭环审计测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import Client
from openpyxl import load_workbook  # type: ignore[import-untyped]

from adapter.skill.execution import PythonScriptExecutor
from app.skill.package import SkillPackageValidator, validate_parameter_schema
from domain.skill.execution import JsonValue, ScriptExecutionRequest
from infrastructure.mcp.stdio import McpStdioTransport
from mcp_servers.hroa.state import HroaStateStore

REPO_ROOT = Path(__file__).resolve().parents[3]
HROA_DIR = REPO_ROOT / "mcp_servers" / "hroa"
HROA_SERVER_PATH = HROA_DIR / "mcp_server.py"
TRAVEL_SKILL_ZIP_PATH = (
    REPO_ROOT
    / "datasets"
    / "enterprise_hroa"
    / "generated"
    / "skill_packages"
    / "travel-expense-audit-pack.zip"
)


def _extract_package(package_path: Path, working_directory: Path) -> None:
    """将已校验的 SK-7 ZIP 包展开至独立临时沙箱目录。"""
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(working_directory)
    (working_directory / "outputs").mkdir()


def _sheet_rows(workbook_path: Path, sheet_name: str) -> list[dict[str, object]]:
    """读取指定 Excel 工作表为以首行表头为键的行字典列表。"""
    workbook = load_workbook(workbook_path, data_only=True, read_only=True)
    try:
        sheet = workbook[sheet_name]
        iterator = sheet.iter_rows(values_only=True)
        headers = tuple(str(value) for value in next(iterator))
        return [
            {header: value for header, value in zip(headers, row, strict=True)} for row in iterator
        ]
    finally:
        workbook.close()


def test_sk7_zip_package_structure_and_manifest_validation() -> None:
    """验证 SK-7 ZIP 根目录直含 SKILL.md、脚本与规则参考，且完全通过 Athena 校验器。"""
    assert TRAVEL_SKILL_ZIP_PATH.is_file()
    with zipfile.ZipFile(TRAVEL_SKILL_ZIP_PATH) as archive:
        names = set(archive.namelist())
    assert names == {
        "SKILL.md",
        "scripts/audit_travel_expenses.py",
        "references/travel-audit-rules.md",
    }

    validated = SkillPackageValidator().validate(TRAVEL_SKILL_ZIP_PATH)
    assert validated.manifest.slug == "travel-expense-audit-pack"
    assert validated.manifest.dependencies == ("openpyxl>=3.1",)
    assert len(validated.manifest.scripts) == 1
    assert validated.manifest.scripts[0].path == "scripts/audit_travel_expenses.py"
    assert validated.manifest.scripts[0].parameters["required"] == [
        "month",
        "department_id",
        "travel_inputs",
    ]


def test_sk7_end_to_end_with_real_fin_ops_mcp_and_unknown_department_failure(
    tmp_path: Path,
) -> None:
    """串联真实 fin_ops stdio MCP 与 PythonScriptExecutor 生成四表 Excel 并核验金额与拦截。"""
    state_file = tmp_path / "fin-ops-skill-state.json"

    async def scenario() -> None:
        transport = McpStdioTransport(
            sys.executable,
            (
                str(HROA_SERVER_PATH),
                "--profile",
                "fin_ops",
                "--state-file",
                str(state_file),
            ),
            cwd=str(HROA_DIR),
            base_environment={"PATH": os.defpath},
            maximum_line_bytes=2 * 1024 * 1024,
            maximum_total_bytes=2 * 1024 * 1024,
        )
        async with Client(
            transport,
            mode="auto",
            read_timeout_seconds=10,
            cache=None,
        ) as client:
            travel_res = await client.call_tool(
                "hroa_list_travel_expenses",
                {"month": "2026-11", "department_id": "ALL"},
            )
            assert travel_res.is_error is False
            travel_inputs = cast(dict[str, object], travel_res.structured_content)

        validated = SkillPackageValidator().validate(TRAVEL_SKILL_ZIP_PATH)
        script_schema = validated.manifest.scripts[0].parameters

        # 使用真实 Draft 2020-12 校验器核对 fin_ops MCP structuredContent 与 SKILL.md 契约一致。
        schema_error = validate_parameter_schema(
            script_schema,
            {
                "month": "2026-11",
                "department_id": "ALL",
                "travel_inputs": travel_inputs,
            },
        )
        assert schema_error is None, f"travel_inputs_schema_mismatch:{schema_error}"

        # 1. 全公司（ALL）端到端审计 6 笔基线差旅单，核验四工作表与精确超标/违规/备用金阻断。
        work_all = tmp_path / "exec-travel-all"
        work_all.mkdir()
        _extract_package(TRAVEL_SKILL_ZIP_PATH, work_all)
        req_all = ScriptExecutionRequest(
            working_directory=work_all,
            script_path="scripts/audit_travel_expenses.py",
            parameters={
                "month": "2026-11",
                "department_id": "ALL",
                "travel_inputs": cast(JsonValue, travel_inputs),
            },
            input_files={},
            output_directory=work_all / "outputs",
            run_id="run_travel_audit_all",
            tool_call_id="call_travel_audit_all",
            worker_instance_id="worker_travel_audit_all",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_all) as execution:
            assert execution.succeeded, execution.stderr.decode(errors="replace")
            outputs = tuple(execution.outputs)
            assert [item.path for item in outputs] == [
                "travel-audit-2026-11.xlsx",
                "travel-audit-2026-11-summary.txt",
            ]
            stdout_payload = json.loads(execution.stdout)
            assert stdout_payload["total_expenses"] == 6
            assert stdout_payload["compliant_count"] == 2
            assert stdout_payload["violation_count"] == 3
            assert stdout_payload["advance_alert_count"] == 1
            assert stdout_payload["total_over_limit_cny"] == 840.0

            workbook_path = outputs[0].source_path
            wb = load_workbook(workbook_path, read_only=True)
            try:
                assert wb.sheetnames == [
                    "审计总览",
                    "超标与违规明细",
                    "合规放行清单",
                    "备用金逾期预警",
                ]
            finally:
                wb.close()

            viol_rows = _sheet_rows(workbook_path, "超标与违规明细")
            pass_rows = _sheet_rows(workbook_path, "合规放行清单")
            adv_rows = _sheet_rows(workbook_path, "备用金逾期预警")

            # 精准校验 3 笔超标/违规单据（540.00、300.00 与公务舱违规）。
            assert {str(r["报销单号"]) for r in viol_rows} == {
                "TRV-2026-1102",
                "TRV-2026-1103",
                "TRV-2026-1104",
            }
            trv_1102 = next(r for r in viol_rows if r["报销单号"] == "TRV-2026-1102")
            assert trv_1102["住宿超标(元)"] == 540.0
            assert trv_1102["合计超标(元)"] == 540.0
            assert trv_1102["稽核判定"] == "超标拦截"

            trv_1103 = next(r for r in viol_rows if r["报销单号"] == "TRV-2026-1103")
            assert trv_1103["住宿超标(元)"] == 210.0
            assert trv_1103["餐补超标(元)"] == 90.0
            assert trv_1103["合计超标(元)"] == 300.0
            assert trv_1103["稽核判定"] == "双项超标拦截"

            trv_1104 = next(r for r in viol_rows if r["报销单号"] == "TRV-2026-1104")
            assert trv_1104["交通舱位"] == "business"
            assert trv_1104["交通金额(元)"] == 4200.0
            assert trv_1104["稽核判定"] == "舱位违规拦截"

            # 精准校验 2 笔合规放行单据（TRV-2026-1101 / TRV-2026-1106）。
            assert {str(r["报销单号"]) for r in pass_rows} == {
                "TRV-2026-1101",
                "TRV-2026-1106",
            }

            # 精准校验 1 笔离职备用金阻断预警（TRV-2026-1105 / FIN-ADV-2026-0914 / 2,400.00）。
            assert len(adv_rows) == 1
            trv_1105 = adv_rows[0]
            assert trv_1105["报销单号"] == "TRV-2026-1105"
            assert trv_1105["备用金单号"] == "FIN-ADV-2026-0914"
            assert trv_1105["未核销金额(元)"] == 2400.0
            assert trv_1105["预警结论"] == "离职备用金阻断预警"

            # 核对 UTF-8 风险摘要文本包含全部关键锚点与金额。
            summary_text = outputs[1].source_path.read_text(encoding="utf-8")
            for must_have in (
                "TRV-2026-1102",
                "540.00",
                "TRV-2026-1103",
                "300.00",
                "TRV-2026-1104",
                "舱位违规",
                "TRV-2026-1105",
                "FIN-ADV-2026-0914",
                "2,400.00",
                "TRV-2026-1101",
                "TRV-2026-1106",
            ):
                assert must_have in summary_text, f"summary_missing:{must_have}"

        # 2. 单部门过滤（dept_mock_platform）仅保留平台架构组 3 笔差旅单，无跨部门泄漏。
        work_plat = tmp_path / "exec-travel-platform"
        work_plat.mkdir()
        _extract_package(TRAVEL_SKILL_ZIP_PATH, work_plat)
        req_plat = ScriptExecutionRequest(
            working_directory=work_plat,
            script_path="scripts/audit_travel_expenses.py",
            parameters={
                "month": "2026-11",
                "department_id": "dept_mock_platform",
                "travel_inputs": cast(JsonValue, travel_inputs),
            },
            input_files={},
            output_directory=work_plat / "outputs",
            run_id="run_travel_audit_plat",
            tool_call_id="call_travel_audit_plat",
            worker_instance_id="worker_travel_audit_plat",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_plat) as exec_plat:
            assert exec_plat.succeeded, exec_plat.stderr.decode(errors="replace")
            plat_stdout = json.loads(exec_plat.stdout)
            assert plat_stdout["total_expenses"] == 3
            assert plat_stdout["compliant_count"] == 1
            assert plat_stdout["violation_count"] == 1
            assert plat_stdout["advance_alert_count"] == 1

        # 3. 不存在部门必须以 department_not_found 明确失败，杜绝空报告。
        work_bad = tmp_path / "exec-travel-bad-dept"
        work_bad.mkdir()
        _extract_package(TRAVEL_SKILL_ZIP_PATH, work_bad)
        req_bad = ScriptExecutionRequest(
            working_directory=work_bad,
            script_path="scripts/audit_travel_expenses.py",
            parameters={
                "month": "2026-11",
                "department_id": "dept_nonexistent_999",
                "travel_inputs": cast(JsonValue, travel_inputs),
            },
            input_files={},
            output_directory=work_bad / "outputs",
            run_id="run_travel_audit_bad",
            tool_call_id="call_travel_audit_bad",
            worker_instance_id="worker_travel_audit_bad",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_bad) as exec_bad:
            assert not exec_bad.succeeded
            assert "department_not_found" in exec_bad.stderr.decode(errors="replace")

    asyncio.run(scenario())


def _load_travel_script_module() -> Any:
    """动态加载 SK-7 脚本模块以便直接核验领域异常。"""
    script_path = (
        REPO_ROOT
        / "datasets"
        / "enterprise_hroa"
        / "skills"
        / "travel_expense_audit"
        / "scripts"
        / "audit_travel_expenses.py"
    )
    spec = importlib.util.spec_from_file_location("sk7_audit_travel_expenses", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bad_month", ["2026-13", "2026/11"])
def test_sk7_schema_and_script_reject_invalid_month_and_mismatch(
    bad_month: str,
    tmp_path: Path,
) -> None:
    """使用真实 JSON Schema 校验器、脚本函数与沙箱子进程三层拦截非法月份与月份不一致。"""
    validated = SkillPackageValidator().validate(TRAVEL_SKILL_ZIP_PATH)
    schema = validated.manifest.scripts[0].parameters
    sample_inputs = HroaStateStore(tmp_path / "schema-travel-state.json").list_travel_expenses(
        month="2026-11", department_id="ALL"
    )

    # 1. Draft 2020-12 JSON Schema 层拦截非法 month。
    schema_err = validate_parameter_schema(
        schema,
        {
            "month": bad_month,
            "department_id": "ALL",
            "travel_inputs": sample_inputs,
        },
    )
    assert schema_err is not None
    assert "month" in schema_err

    # 2. 脚本领域函数层以 month_invalid 拦截非法 travel_inputs.month。
    travel_module = _load_travel_script_module()
    bad_payload = dict(sample_inputs)
    bad_payload["month"] = bad_month
    with pytest.raises(travel_module.TravelAuditInputError, match="month_invalid"):
        travel_module.build_travel_audit_report(bad_payload, "ALL")

    # 3. 真实沙箱子进程拦截 parameters.month 与 travel_inputs.month 不一致。
    work_mismatch = tmp_path / f"exec-travel-mismatch-{bad_month.replace('/', '_')}"
    work_mismatch.mkdir()
    _extract_package(TRAVEL_SKILL_ZIP_PATH, work_mismatch)

    async def run_mismatch() -> None:
        req = ScriptExecutionRequest(
            working_directory=work_mismatch,
            script_path="scripts/audit_travel_expenses.py",
            parameters={
                "month": "2026-12",
                "department_id": "ALL",
                "travel_inputs": cast(JsonValue, sample_inputs),
            },
            input_files={},
            output_directory=work_mismatch / "outputs",
            run_id="run_travel_month_mismatch",
            tool_call_id="call_travel_month_mismatch",
            worker_instance_id="worker_travel_month_mismatch",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req) as execution:
            assert not execution.succeeded
            assert "parameters.month_mismatch" in execution.stderr.decode(errors="replace")

    asyncio.run(run_mismatch())


def test_sk7_offboarding_advance_within_15_workdays_omits_overdue_phrase(tmp_path: Path) -> None:
    """离职未核销备用金但返程未满 15 工作日时，仅提示离职红线且不误报超 15 工作日。"""
    travel_module = _load_travel_script_module()
    sample_inputs = HroaStateStore(tmp_path / "offb-adv-state.json").list_travel_expenses(
        month="2026-11", department_id="dept_mock_platform"
    )
    expenses = cast(list[dict[str, object]], sample_inputs["expenses"])
    trv_1105 = next(exp for exp in expenses if exp["expense_id"] == "TRV-2026-1105")
    trv_1105["workdays_since_return"] = 5

    report = travel_module.build_travel_audit_report(sample_inputs, "dept_mock_platform")
    audited = next(
        item for item in report.advance_alert_items if item.expense_id == "TRV-2026-1105"
    )
    reason_text = "；".join(audited.violation_reasons)
    assert "离职前 5 工作日清零红线" in reason_text
    assert "超 15 工作日红线" not in reason_text
