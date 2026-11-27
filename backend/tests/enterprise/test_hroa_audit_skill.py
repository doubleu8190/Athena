"""SK-5（hroa-lifecycle-audit-pack）ZIP 包结构与真实 hr_ops MCP 闭环执行测试。"""

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
SKILL_ZIP_PATH = (
    REPO_ROOT
    / "datasets"
    / "enterprise_hroa"
    / "generated"
    / "skill_packages"
    / "hroa-lifecycle-audit-pack.zip"
)


def _extract_package(package_path: Path, working_directory: Path) -> None:
    """将已校验的 SK-5 ZIP 包展开至独立临时执行目录。"""
    with zipfile.ZipFile(package_path) as archive:
        archive.extractall(working_directory)
    (working_directory / "outputs").mkdir()


def _sheet_rows(workbook_path: Path, sheet_name: str) -> list[dict[str, object]]:
    """读取 Excel 工作表为以表头为键的行列表。"""
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


def test_sk5_zip_package_structure_and_manifest_validation() -> None:
    """验证 SK-5 ZIP 根目录直含 SKILL.md、脚本与规则参考，且完全通过 Athena 校验器。"""
    assert SKILL_ZIP_PATH.is_file()
    with zipfile.ZipFile(SKILL_ZIP_PATH) as archive:
        names = set(archive.namelist())
    assert names == {
        "SKILL.md",
        "scripts/build_hroa_audit.py",
        "references/hroa-audit-rules.md",
    }

    validated = SkillPackageValidator().validate(SKILL_ZIP_PATH)
    assert validated.manifest.slug == "hroa-lifecycle-audit-pack"
    assert validated.manifest.dependencies == ("openpyxl>=3.1",)
    assert len(validated.manifest.scripts) == 1
    assert validated.manifest.scripts[0].path == "scripts/build_hroa_audit.py"
    assert validated.manifest.scripts[0].parameters["required"] == [
        "month",
        "department_id",
        "hroa_inputs",
    ]


def test_sk5_end_to_end_with_real_hr_ops_mcp_and_unknown_department_failure(
    tmp_path: Path,
) -> None:
    """串联真实 hr_ops stdio MCP 与 PythonScriptExecutor 生成四表 Excel，并验证无效部门拒单。"""
    state_file = tmp_path / "hroa-skill-state.json"

    async def scenario() -> None:
        transport = McpStdioTransport(
            sys.executable,
            (
                str(HROA_SERVER_PATH),
                "--profile",
                "hr_ops",
                "--state-file",
                str(state_file),
            ),
            cwd=str(HROA_DIR),
            base_environment={"PATH": os.defpath},
        )
        async with Client(
            transport,
            mode="auto",
            read_timeout_seconds=10,
            cache=None,
        ) as client:
            roster_res = await client.call_tool(
                "hroa_list_department_roster",
                {"department_id": "ALL", "month": "2026-11"},
            )
            assert roster_res.is_error is False
            hroa_inputs = cast(dict[str, object], roster_res.structured_content)

        validated = SkillPackageValidator().validate(SKILL_ZIP_PATH)
        script_schema = validated.manifest.scripts[0].parameters

        # 使用真实 Draft 2020-12 校验器核对 MCP structuredContent 与 SKILL.md 契约匹配。
        schema_error = validate_parameter_schema(
            script_schema,
            {
                "month": "2026-11",
                "department_id": "ALL",
                "hroa_inputs": hroa_inputs,
            },
        )
        assert schema_error is None, f"hroa_inputs_schema_mismatch:{schema_error}"

        # 1. 全公司（ALL）端到端生成四表 Excel 与 UTF-8 风险摘要。
        work_all = tmp_path / "exec-all"
        work_all.mkdir()
        _extract_package(SKILL_ZIP_PATH, work_all)
        req_all = ScriptExecutionRequest(
            working_directory=work_all,
            script_path="scripts/build_hroa_audit.py",
            parameters={
                "month": "2026-11",
                "department_id": "ALL",
                "hroa_inputs": cast(JsonValue, hroa_inputs),
            },
            input_files={},
            output_directory=work_all / "outputs",
            run_id="run_hroa_audit_all",
            tool_call_id="call_hroa_audit_all",
            worker_instance_id="worker_hroa_audit_all",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_all) as execution:
            assert execution.succeeded, execution.stderr.decode(errors="replace")
            outputs = tuple(execution.outputs)
            assert [item.path for item in outputs] == [
                "hroa-audit-2026-11.xlsx",
                "hroa-audit-2026-11-summary.txt",
            ]
            stdout_payload = json.loads(execution.stdout)
            assert stdout_payload["total_employees"] == 14
            assert stdout_payload["department_id"] == "ALL"

            workbook_path = outputs[0].source_path
            wb = load_workbook(workbook_path, read_only=True)
            try:
                assert wb.sheetnames == [
                    "汇总总览",
                    "入职与试用期跟踪",
                    "离职清查与阻断项",
                    "异动与假勤预警",
                ]
            finally:
                wb.close()

            onb_rows = _sheet_rows(workbook_path, "入职与试用期跟踪")
            off_rows = _sheet_rows(workbook_path, "离职清查与阻断项")
            mov_rows = _sheet_rows(workbook_path, "异动与假勤预警")

            assert {str(r["员工工号"]) for r in onb_rows} == {"XC1001", "XC1002"}
            assert {str(r["员工工号"]) for r in off_rows} == {"XC1003", "XC1006"}
            xc1003_row = next(r for r in off_rows if r["员工工号"] == "XC1003")
            assert xc1003_row["可开离职证明"] == "否"
            assert xc1003_row["未休法定年假(天)"] == 3.5
            assert "IT-MBP-2023-088" in str(xc1003_row["阻断明细"])
            assert "2,400.00" in str(xc1003_row["阻断明细"])

            assert {"XC1004", "XC1005", "XC1007", "XC1008"} <= {
                str(r["员工工号"]) for r in mov_rows
            }

            summary_text = outputs[1].source_path.read_text(encoding="utf-8")
            for must_have in (
                "XC1001 陈思远",
                "XC1002 许知夏",
                "XC1003 韩景明",
                "XC1004 郑语桐",
                "XC1005 高明轩",
                "IT-MBP-2023-088",
            ):
                assert must_have in summary_text

        # 2. 单部门过滤（dept_mock_platform）仅保留平台架构组员工，无跨部门泄漏。
        work_plat = tmp_path / "exec-platform"
        work_plat.mkdir()
        _extract_package(SKILL_ZIP_PATH, work_plat)
        req_plat = ScriptExecutionRequest(
            working_directory=work_plat,
            script_path="scripts/build_hroa_audit.py",
            parameters={
                "month": "2026-11",
                "department_id": "dept_mock_platform",
                "hroa_inputs": cast(JsonValue, hroa_inputs),
            },
            input_files={},
            output_directory=work_plat / "outputs",
            run_id="run_hroa_audit_plat",
            tool_call_id="call_hroa_audit_plat",
            worker_instance_id="worker_hroa_audit_plat",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_plat) as exec_plat:
            assert exec_plat.succeeded, exec_plat.stderr.decode(errors="replace")
            plat_stdout = json.loads(exec_plat.stdout)
            assert plat_stdout["total_employees"] == 4
            plat_outputs = tuple(exec_plat.outputs)
            plat_onb = _sheet_rows(plat_outputs[0].source_path, "入职与试用期跟踪")
            assert {str(r["员工工号"]) for r in plat_onb} == {"XC1001"}

        # 3. 不存在的部门编号必须以 department_not_found 明确失败，不得生成空报告。
        work_bad = tmp_path / "exec-bad-dept"
        work_bad.mkdir()
        _extract_package(SKILL_ZIP_PATH, work_bad)
        req_bad = ScriptExecutionRequest(
            working_directory=work_bad,
            script_path="scripts/build_hroa_audit.py",
            parameters={
                "month": "2026-11",
                "department_id": "dept_unknown_999",
                "hroa_inputs": cast(JsonValue, hroa_inputs),
            },
            input_files={},
            output_directory=work_bad / "outputs",
            run_id="run_hroa_audit_bad",
            tool_call_id="call_hroa_audit_bad",
            worker_instance_id="worker_hroa_audit_bad",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_bad) as exec_bad:
            assert not exec_bad.succeeded
            assert "department_not_found" in exec_bad.stderr.decode(errors="replace")

    asyncio.run(scenario())


def _load_audit_script_module() -> Any:
    """动态加载 SK-5 脚本模块以便直接校验脚本层领域异常。"""
    script_path = (
        REPO_ROOT
        / "datasets"
        / "enterprise_hroa"
        / "skills"
        / "hroa_lifecycle_audit"
        / "scripts"
        / "build_hroa_audit.py"
    )
    spec = importlib.util.spec_from_file_location("sk5_build_hroa_audit", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bad_month", ["2026-13", "2026/11"])
def test_sk5_schema_and_script_reject_invalid_month_and_mismatch(
    bad_month: str,
    tmp_path: Path,
) -> None:
    """使用真实 JSON Schema 校验器、脚本函数与子进程执行双重拦截非法月份与月份不一致。"""
    script_path = (
        REPO_ROOT
        / "datasets"
        / "enterprise_hroa"
        / "skills"
        / "hroa_lifecycle_audit"
        / "scripts"
        / "build_hroa_audit.py"
    )
    compile(script_path.read_text(encoding="utf-8"), script_path.name, "exec")

    validated = SkillPackageValidator().validate(SKILL_ZIP_PATH)
    schema = validated.manifest.scripts[0].parameters
    sample_inputs = HroaStateStore(tmp_path / "schema-state.json").list_department_roster(
        department_id="ALL", month="2026-11"
    )

    # 1. JSON Schema 层必须拒绝非法 month 参数。
    schema_err = validate_parameter_schema(
        schema,
        {
            "month": bad_month,
            "department_id": "ALL",
            "hroa_inputs": sample_inputs,
        },
    )
    assert schema_err is not None
    assert "month" in schema_err

    # 2. 脚本核心函数层同样必须以 month_invalid 拒绝非法 hroa_inputs.month。
    audit_module = _load_audit_script_module()
    bad_payload = dict(sample_inputs)
    bad_payload["month"] = bad_month
    with pytest.raises(audit_module.HroaAuditInputError, match="month_invalid"):
        audit_module.build_hroa_audit_report(bad_payload, "ALL")

    # 3. 真实子进程执行：parameters.month 与 hroa_inputs.month 不一致须报 month_mismatch。
    work_mismatch = tmp_path / f"exec-mismatch-{bad_month.replace('/', '_')}"
    work_mismatch.mkdir()
    _extract_package(SKILL_ZIP_PATH, work_mismatch)

    async def run_mismatch() -> None:
        req = ScriptExecutionRequest(
            working_directory=work_mismatch,
            script_path="scripts/build_hroa_audit.py",
            parameters={
                "month": "2026-12",
                "department_id": "ALL",
                "hroa_inputs": cast(JsonValue, sample_inputs),
            },
            input_files={},
            output_directory=work_mismatch / "outputs",
            run_id="run_hroa_month_mismatch",
            tool_call_id="call_hroa_month_mismatch",
            worker_instance_id="worker_hroa_month_mismatch",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req) as execution:
            assert not execution.succeeded
            assert "parameters.month_mismatch" in execution.stderr.decode(errors="replace")

    asyncio.run(run_mismatch())
