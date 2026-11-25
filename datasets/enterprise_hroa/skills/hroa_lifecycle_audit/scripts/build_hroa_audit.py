#!/usr/bin/env python3
"""星辰智联入转调离月度风控稽核台账生成器（SK-5 Skill 入口脚本）。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]

MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
CLEARANCE_KEYS = ("work_handover", "it_asset", "finance_advance", "account_access")

type JsonObject = dict[str, object]


class HroaAuditInputError(ValueError):
    """表示 HR/OA 稽核输入不符合 Skill 参数契约。"""


@dataclass(frozen=True, slots=True)
class AuditAlert:
    """一条供摘要文件与汇总表输出的阻断或预警记录。"""

    severity: str
    category: str
    employee_no: str
    employee_name: str
    department_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """一次月度稽核的结构化分析结果。"""

    month: str
    baseline_date: str
    department_id: str
    department_label: str
    total_employees: int
    status_counts: dict[str, int]
    onboarding_rows: tuple[tuple[object, ...], ...]
    offboarding_rows: tuple[tuple[object, ...], ...]
    movement_leave_rows: tuple[tuple[object, ...], ...]
    alerts: tuple[AuditAlert, ...]
    year_end_carryover_summary: str | None = None
    year_end_carryover_note: str | None = None


def _read_object(value: object, location: str) -> JsonObject:
    """将 JSON 节点收窄为字符串键字典。"""
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise HroaAuditInputError(f"{location}_invalid")
    return cast(JsonObject, value)


def _read_records(payload: JsonObject, key: str) -> list[JsonObject]:
    """读取对象数组字段并逐条校验。"""
    value = payload.get(key)
    if not isinstance(value, list):
        raise HroaAuditInputError(f"{key}_invalid")
    return [_read_object(item, f"{key}[{idx}]") for idx, item in enumerate(value)]


def _required_text(record: JsonObject, key: str, location: str) -> str:
    """读取必填非空字符串。"""
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HroaAuditInputError(f"{location}.{key}_invalid")
    return value.strip()


def _text(record: JsonObject, key: str, default: str = "") -> str:
    """安全读取可选字符串字段。"""
    value = record.get(key)
    if isinstance(value, str):
        return value.strip()
    return default


def _number(record: JsonObject, key: str, default: float = 0.0) -> float:
    """安全读取可选数值字段并拒绝布尔值。"""
    value = record.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _days_until(base_iso: str, target_iso: str) -> int | None:
    """计算目标日期距基准日的自然日差值。"""
    if not base_iso or not target_iso:
        return None
    try:
        return (date.fromisoformat(target_iso) - date.fromisoformat(base_iso)).days
    except ValueError:
        return None


def build_hroa_audit_report(payload: JsonObject, department_id: str) -> AuditReport:
    """校验 HR/OA 聚合数据并按目标部门（或 ALL）构建四表风控稽核结果。"""
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise HroaAuditInputError("schema_version_unsupported")
    month = _required_text(payload, "month", "hroa_inputs")
    if MONTH_PATTERN.fullmatch(month) is None:
        raise HroaAuditInputError("month_invalid")
    baseline_date = _text(payload, "baseline_date", "2026-11-24")
    target_dept = department_id.strip()
    if not target_dept:
        raise HroaAuditInputError("department_id_invalid")

    departments = _read_records(payload, "departments")
    dept_names = {
        _required_text(item, "department_id", "departments"): _required_text(
            item, "department_name", "departments"
        )
        for item in departments
    }
    all_employees = _read_records(payload, "employees")
    all_balances = _read_records(payload, "leave_balances")
    all_tickets = _read_records(payload, "oa_tickets")
    all_clearances = _read_records(payload, "offboarding_clearances")

    if target_dept == "ALL":
        scoped_employees = list(all_employees)
        department_label = "全公司（ALL）"
    else:
        scoped_employees = [
            emp for emp in all_employees if _text(emp, "department_id") == target_dept
        ]
        # 指定部门不存在于花名册且无任何员工记录时明确报错，杜绝生成误导性空报告。
        if not scoped_employees:
            raise HroaAuditInputError("department_not_found")
        department_label = f"{dept_names.get(target_dept, target_dept)}（{target_dept}）"

    scoped_emp_nos = {_required_text(emp, "employee_no", "employees") for emp in scoped_employees}
    emp_by_no = {_required_text(emp, "employee_no", "employees"): emp for emp in scoped_employees}
    balance_by_no = {
        _required_text(bal, "employee_no", "leave_balances"): bal
        for bal in all_balances
        if _text(bal, "employee_no") in scoped_emp_nos
    }
    clearance_by_no = {
        _required_text(clr, "employee_no", "offboarding_clearances"): clr
        for clr in all_clearances
        if _text(clr, "employee_no") in scoped_emp_nos
    }
    scoped_tickets = [
        ticket for ticket in all_tickets if _text(ticket, "employee_no") in scoped_emp_nos
    ]

    status_counts: dict[str, int] = {
        "active": 0,
        "preboarding": 0,
        "probation": 0,
        "offboarding": 0,
        "resigned": 0,
    }
    for emp in scoped_employees:
        emp_status = _text(emp, "employment_status", "active")
        status_counts[emp_status] = status_counts.get(emp_status, 0) + 1

    alerts: list[AuditAlert] = []
    onboarding_rows: list[tuple[object, ...]] = []
    offboarding_rows: list[tuple[object, ...]] = []
    movement_leave_rows: list[tuple[object, ...]] = []

    # 1. 构建《入职与试用期跟踪》明细与临期预警。
    for emp_no in sorted(emp_by_no):
        emp = emp_by_no[emp_no]
        emp_status = _text(emp, "employment_status")
        if emp_status not in {"preboarding", "probation"}:
            continue
        emp_name = _text(emp, "name")
        dept_name = _text(emp, "department_name")
        bal = balance_by_no.get(emp_no, {})
        sick_used = _number(bal, "sick_used_days")
        prob_end = _text(emp, "probation_end_date")
        days_left = _days_until(baseline_date, prob_end) if emp_status == "probation" else None
        if emp_status == "preboarding":
            stage = "待入职筹备"
            risk_note = (
                f"Offer {_text(emp, 'offer_code') or '-'}，报到日 {_text(emp, 'hire_date')}，"
                f"导师 {_text(emp, 'mentor_no') or '-'}，配机 {_text(emp, 'laptop_model') or '-'}"
            )
            alerts.append(
                AuditAlert(
                    severity="待办提醒",
                    category="入职筹备",
                    employee_no=emp_no,
                    employee_name=emp_name,
                    department_name=dept_name,
                    reason=risk_note,
                )
            )
        else:
            stage = "试用期待转正"
            days_text = f"距到期 {days_left} 天" if days_left is not None else "待核验到期日"
            risk_note = (
                f"试用期 {prob_end} 到期（{days_text}），已用病假 {sick_used:g} 天，"
                f"主管 {_text(emp, 'manager_no') or '-'} 待完成转正答辩"
            )
            if days_left is not None and days_left <= 15:
                alerts.append(
                    AuditAlert(
                        severity="高优先预警",
                        category="试用期临期转正",
                        employee_no=emp_no,
                        employee_name=emp_name,
                        department_name=dept_name,
                        reason=risk_note,
                    )
                )
        onboarding_rows.append(
            (
                emp_no,
                emp_name,
                dept_name,
                _text(emp, "job_title"),
                stage,
                _text(emp, "offer_code") or "-",
                _text(emp, "hire_date"),
                prob_end or "-",
                _text(emp, "mentor_no") or "-",
                _text(emp, "laptop_model") or "-",
                sick_used,
                risk_note,
            )
        )

    # 2. 构建《离职清查与阻断项》明细与阻断告警。
    for emp_no in sorted(emp_by_no):
        emp = emp_by_no[emp_no]
        emp_status = _text(emp, "employment_status")
        if emp_status not in {"offboarding", "resigned"} and emp_no not in clearance_by_no:
            continue
        emp_name = _text(emp, "name")
        dept_name = _text(emp, "department_name")
        clr = clearance_by_no.get(emp_no, {})
        items = _read_object(clr.get("items"), "clearance.items") if clr.get("items") else {}
        item_statuses = {
            key: _text(_read_object(items.get(key), f"items.{key}"), "status", "pending")
            if isinstance(items.get(key), dict)
            else "pending"
            for key in CLEARANCE_KEYS
        }
        blocking_details: list[str] = []
        for key in CLEARANCE_KEYS:
            raw_item = items.get(key)
            if not isinstance(raw_item, dict):
                continue
            item_obj = cast(JsonObject, raw_item)
            if _text(item_obj, "status") == "blocked":
                blocking_details.append(f"{_text(item_obj, 'label')}：{_text(item_obj, 'detail')}")
        can_issue = bool(clr.get("can_issue_certificate", False))
        unused_statutory = _number(clr, "unused_statutory_annual_days")
        blocking_text = "；".join(blocking_details) if blocking_details else "无阻断项"
        if emp_status == "offboarding":
            if blocking_details or not can_issue:
                alerts.append(
                    AuditAlert(
                        severity="严重阻断" if blocking_details else "待闭环",
                        category="离职清查阻断",
                        employee_no=emp_no,
                        employee_name=emp_name,
                        department_name=dept_name,
                        reason=(
                            f"最后工作日 {_text(clr, 'last_working_day')}，"
                            f"未休法定年假 {unused_statutory:g} 天，"
                            f"不可开具离职证明；{blocking_text}"
                        ),
                    )
                )
        offboarding_rows.append(
            (
                emp_no,
                emp_name,
                dept_name,
                emp_status,
                _text(clr, "last_working_day") or _text(emp, "last_working_day") or "-",
                unused_statutory,
                item_statuses["work_handover"],
                item_statuses["it_asset"],
                item_statuses["finance_advance"],
                item_statuses["account_access"],
                "是" if can_issue else "否",
                blocking_text,
            )
        )

    # 3. 构建《异动与假勤预警》明细（覆盖调岗、晋升、超额请假与待核验病假工单）。
    for ticket in scoped_tickets:
        t_type = _text(ticket, "ticket_type")
        if t_type not in {"transfer", "promotion", "leave"}:
            continue
        emp_no = _text(ticket, "employee_no")
        emp_name = _text(ticket, "employee_name")
        dept_name = _text(ticket, "department_name")
        t_status = _text(ticket, "status")
        summary = _text(ticket, "summary")
        bal = balance_by_no.get(emp_no, {})
        annual_rem = _number(bal, "annual_remaining_days")
        comp_rem = _number(bal, "comp_off_remaining_days")

        if t_status == "warning_insufficient_balance":
            risk_level = "高风险预警"
            action = "按 XC-HR-2026-001 拆分为剩余年假 + 可用调休 + 无薪事假"
            alerts.append(
                AuditAlert(
                    severity="高风险预警",
                    category="假勤余额不足",
                    employee_no=emp_no,
                    employee_name=emp_name,
                    department_name=dept_name,
                    reason=f"{summary}（{action}）",
                )
            )
        elif t_status == "pending_attachment_verify":
            risk_level = "凭证待核验"
            action = "HRBP 核验二级甲等及以上医院病假证明后放行"
            alerts.append(
                AuditAlert(
                    severity="待核验",
                    category="病假证明核验",
                    employee_no=emp_no,
                    employee_name=emp_name,
                    department_name=dept_name,
                    reason=summary,
                )
            )
        elif t_type == "transfer":
            risk_level = "异动跟进"
            action = f"三方会签并确认剩余年假 {annual_rem:g} 天全额结转"
            alerts.append(
                AuditAlert(
                    severity="异动提醒",
                    category="跨部门平调",
                    employee_no=emp_no,
                    employee_name=emp_name,
                    department_name=dept_name,
                    reason=summary,
                )
            )
        elif t_type == "promotion":
            risk_level = "晋升待批"
            action = "核验晋升审批与生效日分段计薪"
        else:
            risk_level = "常规跟进"
            action = "按审批链正常流转"

        movement_leave_rows.append(
            (
                _text(ticket, "ticket_id"),
                emp_no,
                emp_name,
                dept_name,
                t_type,
                t_status,
                annual_rem,
                comp_rem,
                risk_level,
                summary,
                action,
            )
        )

    # 解析 2026-12 月度快照携带的可选跨年年假结转规则（2026-11 无此字段时为 None）。
    carryover_summary: str | None = None
    carryover_note: str | None = None
    raw_carryover = payload.get("year_end_carryover_rule")
    if isinstance(raw_carryover, dict):
        carryover_obj = _read_object(raw_carryover, "hroa_inputs.year_end_carryover_rule")
        cap_days = _number(carryover_obj, "statutory_annual_carryover_cap_days", 3.0)
        expire_date = _text(carryover_obj, "statutory_carryover_expire_date", "2027-03-31")
        tenure_allowed = bool(carryover_obj.get("tenure_annual_carryover_allowed", False))
        tenure_policy = "允许结转" if tenure_allowed else "年底清零不结转"
        carryover_summary = f"{cap_days:.1f} 天 / {expire_date}"
        carryover_note = (
            f"法定年假跨年结转上限 {cap_days:.1f} 天（须于 {expire_date} 前使用），"
            f"司龄福利年假{tenure_policy}"
        )

    return AuditReport(
        month=month,
        baseline_date=baseline_date,
        department_id=target_dept,
        department_label=department_label,
        total_employees=len(scoped_employees),
        status_counts=status_counts,
        onboarding_rows=tuple(onboarding_rows),
        offboarding_rows=tuple(offboarding_rows),
        movement_leave_rows=tuple(movement_leave_rows),
        alerts=tuple(alerts),
        year_end_carryover_summary=carryover_summary,
        year_end_carryover_note=carryover_note,
    )


def _style_sheet(sheet: Any, widths: Sequence[int]) -> None:
    """统一设置深蓝表头样式、冻结首行、自动筛选与列宽。"""
    header_fill = PatternFill("solid", fgColor="1B4965")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for idx, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(1, idx).column_letter].width = width


def write_audit_workbook(report: AuditReport, output_directory: Path) -> Path:
    """生成四工作表月度风控稽核 Excel（总览、入职试用、离职清查、异动假勤）。"""
    output_directory.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    # Sheet 1：汇总总览
    summary_sheet = workbook.active
    summary_sheet.title = "汇总总览"
    summary_sheet.append(["稽核指标", "数值/范围", "说明"])
    summary_sheet.append(["稽核月份", report.month, f"基准日期 {report.baseline_date}"])
    summary_sheet.append(["稽核部门范围", report.department_label, report.department_id])
    summary_sheet.append(
        [
            "覆盖总人数",
            report.total_employees,
            "含在职、待入职、试用期、离职办理中及已离职档案",
        ]
    )
    summary_sheet.append(["在职人数 (active)", report.status_counts.get("active", 0), "正式在岗"])
    summary_sheet.append(
        [
            "待入职人数 (preboarding)",
            report.status_counts.get("preboarding", 0),
            "需跟进报到、配机与导师",
        ]
    )
    summary_sheet.append(
        [
            "试用期人数 (probation)",
            report.status_counts.get("probation", 0),
            "需提前 15 工作日发起转正答辩",
        ]
    )
    summary_sheet.append(
        [
            "离职办理中 (offboarding)",
            report.status_counts.get("offboarding", 0),
            "四项清查全 cleared 方可签发离职证明",
        ]
    )
    summary_sheet.append(
        [
            "已离职归档 (resigned)",
            report.status_counts.get("resigned", 0),
            "账号已停用，禁止假勤与异动操作",
        ]
    )
    summary_sheet.append(["风险与阻断提醒总数", len(report.alerts), "详见摘要文本与各明细工作表"])
    if report.year_end_carryover_note:
        summary_sheet.append(
            [
                "跨年年假结转规则",
                report.year_end_carryover_summary or "",
                report.year_end_carryover_note,
            ]
        )
    _style_sheet(summary_sheet, (26, 28, 56))

    # Sheet 2：入职与试用期跟踪
    onb_sheet = workbook.create_sheet("入职与试用期跟踪")
    onb_sheet.append(
        [
            "员工工号",
            "姓名",
            "部门",
            "岗位",
            "阶段",
            "Offer编号",
            "入职日期",
            "试用期到期日",
            "导师工号",
            "配发电脑型号",
            "已用病假(天)",
            "跟进与预警说明",
        ]
    )
    for row in report.onboarding_rows:
        onb_sheet.append(list(row))
    _style_sheet(onb_sheet, (12, 12, 16, 18, 14, 18, 14, 14, 12, 32, 14, 52))

    # Sheet 3：离职清查与阻断项
    off_sheet = workbook.create_sheet("离职清查与阻断项")
    off_sheet.append(
        [
            "员工工号",
            "姓名",
            "部门",
            "档案状态",
            "最后工作日",
            "未休法定年假(天)",
            "工作交接",
            "IT资产",
            "财务备用金",
            "账号权限",
            "可开离职证明",
            "阻断明细",
        ]
    )
    for row in report.offboarding_rows:
        off_sheet.append(list(row))
    _style_sheet(off_sheet, (12, 12, 16, 14, 14, 16, 12, 12, 12, 12, 14, 58))

    # Sheet 4：异动与假勤预警
    mov_sheet = workbook.create_sheet("异动与假勤预警")
    mov_sheet.append(
        [
            "OA工单号",
            "员工工号",
            "姓名",
            "所属部门",
            "工单类型",
            "工单状态",
            "剩余年假(天)",
            "剩余调休(天)",
            "预警级别",
            "工单摘要",
            "合规处置建议",
        ]
    )
    for row in report.movement_leave_rows:
        mov_sheet.append(list(row))
    _style_sheet(mov_sheet, (20, 12, 12, 16, 14, 24, 14, 14, 14, 48, 44))

    output_path = output_directory / f"hroa-audit-{report.month}.xlsx"
    workbook.save(output_path)
    return output_path


def write_audit_summary(report: AuditReport, output_directory: Path) -> Path:
    """生成可供 session_file_read 完整读取的 UTF-8 阻断与预警摘要文本。"""
    output_directory.mkdir(parents=True, exist_ok=True)
    lines = [
        f"星辰智联 {report.month} 入转调离月度风控稽核摘要",
        f"稽核范围：{report.department_label}",
        f"基准日期：{report.baseline_date}",
        (
            f"人员概况：共 {report.total_employees} 人"
            f"（在职 {report.status_counts.get('active', 0)}、"
            f"待入职 {report.status_counts.get('preboarding', 0)}、"
            f"试用期 {report.status_counts.get('probation', 0)}、"
            f"离职办理中 {report.status_counts.get('offboarding', 0)}、"
            f"已离职 {report.status_counts.get('resigned', 0)}）"
        ),
    ]
    if report.year_end_carryover_note:
        lines.append(f"跨年结转口径：{report.year_end_carryover_note}")
    lines.extend(
        [
            "",
            f"阻断与预警明细（共 {len(report.alerts)} 项）：",
        ]
    )
    if report.alerts:
        for alert in report.alerts:
            lines.append(
                f"- [{alert.severity}][{alert.category}] "
                f"{alert.employee_no} {alert.employee_name}"
                f"（{alert.department_name}）：{alert.reason}"
            )
    else:
        lines.append("- 无阻断或高风险预警项")
    summary_path = output_directory / f"hroa-audit-{report.month}-summary.txt"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def _load_request() -> tuple[JsonObject, Path]:
    """从 ATHENA_INPUT_PATH 读取 Skill 执行参数与输出目录。"""
    input_path = Path(os.environ["ATHENA_INPUT_PATH"])
    decoded = json.loads(input_path.read_text(encoding="utf-8"))
    request = _read_object(decoded, "request")
    parameters = _read_object(request.get("parameters"), "parameters")
    output_dir = request.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise HroaAuditInputError("output_dir_invalid")
    return parameters, Path(output_dir)


def main() -> None:
    """Athena Skill 入口：生成四表 Excel、UTF-8 风险摘要及交付 manifest.json。"""
    try:
        parameters, output_directory = _load_request()
        month = _required_text(parameters, "month", "parameters")
        department_id = _required_text(parameters, "department_id", "parameters")
        hroa_inputs = _read_object(parameters.get("hroa_inputs"), "parameters.hroa_inputs")
        report = build_hroa_audit_report(hroa_inputs, department_id)
        if report.month != month:
            raise HroaAuditInputError("parameters.month_mismatch")

        workbook_path = write_audit_workbook(report, output_directory)
        summary_path = write_audit_summary(report, output_directory)
        manifest_path = output_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "outputs": [
                        {
                            "path": workbook_path.name,
                            "label": (
                                f"{report.month} {report.department_id} 入转调离月度风控稽核台账"
                            ),
                        },
                        {
                            "path": summary_path.name,
                            "label": (
                                f"{report.month} {report.department_id} 入转调离风险与阻断摘要"
                            ),
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "month": report.month,
                    "department_id": report.department_id,
                    "total_employees": report.total_employees,
                    "alert_count": len(report.alerts),
                    "workbook": workbook_path.name,
                    "summary": summary_path.name,
                },
                ensure_ascii=False,
            )
        )
    except (
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        HroaAuditInputError,
        ValueError,
    ) as error:
        raise SystemExit(f"hroa_audit_skill_failed:{error}") from error


if __name__ == "__main__":
    main()
