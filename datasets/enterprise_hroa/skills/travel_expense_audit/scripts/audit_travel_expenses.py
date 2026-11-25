#!/usr/bin/env python3
"""星辰智联差旅报销超标与备用金稽核审计生成器（SK-7 Skill 入口脚本）。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]

MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
DEFAULT_HOTEL_LIMITS: dict[str, float] = {
    "tier1": 650.0,
    "tier2": 450.0,
    "other": 350.0,
}
DEFAULT_MEAL_LIMIT = 120.0
DEFAULT_ALLOWED_CABINS = frozenset({"economy", "second_class_rail"})
DEFAULT_ADVANCE_CLEAR_WORKDAYS = 15

type JsonObject = dict[str, object]


class TravelAuditInputError(ValueError):
    """表示差旅审计输入不符合 Skill 参数契约或业务前提。"""


@dataclass(frozen=True, slots=True)
class AuditedExpense:
    """单笔差旅报销单的确定性合规核算结论。"""

    expense_id: str
    employee_no: str
    employee_name: str
    department_id: str
    department_name: str
    employment_status: str
    destination_city: str
    city_tier: str
    hotel_nights: int
    hotel_rate_per_night: float
    hotel_limit_per_night: float
    hotel_over_cny: float
    meal_days: int
    meal_per_day: float
    meal_limit_per_day: float
    meal_over_cny: float
    total_over_cny: float
    transport_cabin: str
    transport_amount_cny: float
    cabin_violation: bool
    advance_id: str
    advance_amount_cny: float
    advance_overdue_or_offboarding: bool
    workdays_since_return: int
    total_claimed_cny: float
    verdict: str
    verdict_label: str
    violation_reasons: tuple[str, ...]
    recommended_action: str


@dataclass(frozen=True, slots=True)
class TravelAuditReport:
    """月度差旅报销稽核全量聚合报告。"""

    month: str
    baseline_date: str
    department_id: str
    department_label: str
    total_expenses: int
    total_claimed_cny: float
    total_over_limit_cny: float
    total_unreconciled_advance_cny: float
    compliant_items: tuple[AuditedExpense, ...]
    violation_items: tuple[AuditedExpense, ...]
    advance_alert_items: tuple[AuditedExpense, ...]
    all_items: tuple[AuditedExpense, ...]


def _read_object(value: object, location: str) -> JsonObject:
    """将 JSON 节点严格收窄为字符串键字典。"""
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise TravelAuditInputError(f"{location}_invalid")
    return cast(JsonObject, value)


def _read_records(payload: JsonObject, key: str) -> list[JsonObject]:
    """读取对象数组字段并逐条校验。"""
    value = payload.get(key)
    if not isinstance(value, list):
        raise TravelAuditInputError(f"{key}_invalid")
    return [_read_object(item, f"{key}[{idx}]") for idx, item in enumerate(value)]


def _required_text(record: JsonObject, key: str, location: str) -> str:
    """读取必填非空字符串。"""
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TravelAuditInputError(f"{location}.{key}_invalid")
    return value.strip()


def _text(record: JsonObject, key: str, default: str = "") -> str:
    """安全读取可选字符串字段。"""
    value = record.get(key)
    if isinstance(value, str):
        return value.strip()
    return default


def _number(record: JsonObject, key: str, default: float = 0.0) -> float:
    """安全读取数值字段并拒绝布尔值混入。"""
    value = record.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _integer(record: JsonObject, key: str, default: int = 0) -> int:
    """安全读取整数字段并拒绝布尔值混入。"""
    value = record.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _parse_standards(raw_standards: JsonObject) -> tuple[dict[str, float], float, set[str], int]:
    """解析输入携带的差旅限额标准，缺失字段回退至 XC-FIN-2026-006 基准红线。"""
    hotel_limits = dict(DEFAULT_HOTEL_LIMITS)
    raw_hotel = raw_standards.get("hotel_limit_by_tier")
    if isinstance(raw_hotel, dict):
        for tier, limit_val in raw_hotel.items():
            if (
                isinstance(tier, str)
                and not isinstance(limit_val, bool)
                and isinstance(limit_val, (int, float))
            ):
                hotel_limits[tier] = float(limit_val)

    meal_limit = _number(raw_standards, "meal_limit_per_day", DEFAULT_MEAL_LIMIT)
    if meal_limit <= 0:
        meal_limit = DEFAULT_MEAL_LIMIT

    allowed_cabins = set(DEFAULT_ALLOWED_CABINS)
    raw_cabins = raw_standards.get("allowed_transport_cabins")
    if isinstance(raw_cabins, list):
        parsed = {str(item).strip().lower() for item in raw_cabins if isinstance(item, str)}
        if parsed:
            allowed_cabins = parsed

    advance_workdays = _integer(
        raw_standards, "advance_clear_workdays", DEFAULT_ADVANCE_CLEAR_WORKDAYS
    )
    if advance_workdays <= 0:
        advance_workdays = DEFAULT_ADVANCE_CLEAR_WORKDAYS

    return hotel_limits, meal_limit, allowed_cabins, advance_workdays


def build_travel_audit_report(payload: JsonObject, department_id: str) -> TravelAuditReport:
    """校验差旅聚合输入并按 XC-FIN-2026-006 / XC-FIN-TRV-021 执行确定性逐笔核算。"""
    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise TravelAuditInputError("schema_version_unsupported")
    month = _required_text(payload, "month", "travel_inputs")
    if MONTH_PATTERN.fullmatch(month) is None:
        raise TravelAuditInputError("month_invalid")
    baseline_date = _text(payload, "baseline_date", "2026-11-24")
    target_dept = department_id.strip()
    if not target_dept:
        raise TravelAuditInputError("department_id_invalid")

    departments = _read_records(payload, "departments")
    dept_names = {
        _required_text(item, "department_id", "departments"): _required_text(
            item, "department_name", "departments"
        )
        for item in departments
    }
    standards_obj = _read_object(payload.get("travel_standards"), "travel_inputs.travel_standards")
    hotel_limits, meal_limit, allowed_cabins, advance_workdays = _parse_standards(standards_obj)
    all_expenses = _read_records(payload, "expenses")

    if target_dept == "ALL":
        scoped_expenses = list(all_expenses)
        department_label = "全公司（ALL）"
    else:
        scoped_expenses = [
            exp for exp in all_expenses if _text(exp, "department_id") == target_dept
        ]
        # 目标部门不存在或在该月无任何差旅报销单时明确报错，杜绝产出空表格。
        if not scoped_expenses:
            raise TravelAuditInputError("department_not_found")
        department_label = f"{dept_names.get(target_dept, target_dept)}（{target_dept}）"

    audited_items: list[AuditedExpense] = []
    for exp in sorted(scoped_expenses, key=lambda item: _text(item, "expense_id")):
        expense_id = _required_text(exp, "expense_id", "expenses")
        emp_no = _required_text(exp, "employee_no", "expenses")
        emp_name = _required_text(exp, "employee_name", "expenses")
        dept_id = _required_text(exp, "department_id", "expenses")
        dept_name = _text(exp, "department_name", dept_names.get(dept_id, dept_id))
        emp_status = _text(exp, "employment_status", "active")
        city = _text(exp, "destination_city")
        city_tier = _text(exp, "city_tier", "other")

        hotel_nights = max(0, _integer(exp, "hotel_nights"))
        hotel_rate = round(_number(exp, "hotel_rate_per_night"), 2)
        hotel_limit = round(hotel_limits.get(city_tier, DEFAULT_HOTEL_LIMITS["other"]), 2)
        hotel_over = round(max(0.0, hotel_rate - hotel_limit) * hotel_nights, 2)

        meal_days = max(0, _integer(exp, "meal_days"))
        meal_per_day = round(_number(exp, "meal_per_day"), 2)
        meal_over = round(max(0.0, meal_per_day - meal_limit) * meal_days, 2)
        total_over = round(hotel_over + meal_over, 2)

        cabin = _text(exp, "transport_cabin", "economy").lower()
        transport_amount = round(_number(exp, "transport_amount_cny"), 2)
        has_special_approval = bool(exp.get("has_special_approval", False))
        cabin_violation = cabin not in allowed_cabins and not has_special_approval

        advance_id = _text(exp, "advance_id")
        advance_amount = round(_number(exp, "advance_amount_cny"), 2)
        advance_unreconciled = bool(exp.get("advance_unreconciled", False))
        workdays_since_return = max(0, _integer(exp, "workdays_since_return"))
        advance_overdue = advance_unreconciled and workdays_since_return > advance_workdays
        advance_offboarding_block = advance_unreconciled and emp_status == "offboarding"
        advance_alert = advance_overdue or advance_offboarding_block

        total_claimed = round(_number(exp, "total_claimed_cny"), 2)

        reasons: list[str] = []
        if hotel_over > 0:
            per_night_over = round(hotel_rate - hotel_limit, 2)
            reasons.append(
                f"住宿超标：{city}（{city_tier}）标准 {hotel_limit:.2f} 元/晚，"
                f"实报 {hotel_rate:.2f} 元/晚（超 {per_night_over:.2f} 元/晚 × {hotel_nights} 晚，"
                f"合计超标 {hotel_over:.2f} 元）"
            )
        if meal_over > 0:
            per_day_over = round(meal_per_day - meal_limit, 2)
            reasons.append(
                f"餐补超标：标准 {meal_limit:.2f} 元/天，"
                f"实报 {meal_per_day:.2f} 元/天（超 {per_day_over:.2f} 元/天 × {meal_days} 天，"
                f"合计超标 {meal_over:.2f} 元）"
            )
        if cabin_violation:
            reasons.append(
                f"舱位违规：未附特批升级 {cabin} 舱位（机票/交通 {transport_amount:,.2f} 元）"
            )
        if advance_alert:
            # 按逾期超 15 工作日与离职前 5 工作日红线分别精准拼接原因，避免未超期离职单误报超期。
            alert_clauses: list[str] = []
            if advance_overdue:
                alert_clauses.append(
                    f"返程已 {workdays_since_return} 个工作日（超 {advance_workdays} 工作日红线）"
                )
            if advance_offboarding_block:
                alert_clauses.append("处于离职前 5 工作日清零红线")
            reasons.append(
                f"备用金阻断预警：关联未核销备用金 {advance_id or '-'} "
                f"金额 {advance_amount:,.2f} 元，{'且'.join(alert_clauses)}"
            )

        if advance_offboarding_block:
            verdict = "offboarding_advance_blocked"
            verdict_label = "离职备用金阻断预警"
            action = (
                f"冻结报销付款并联动 HR 保持 finance_advance=blocked，"
                f"要求离职前 5 工作日内贴票冲账或退回 {advance_amount:,.2f} 元"
            )
        elif cabin_violation:
            verdict = "rejected_cabin_violation"
            verdict_label = "舱位违规拦截"
            action = (
                "按 XC-FIN-2026-006 驳回公务舱/头等舱差额，"
                "仅核定同航班经济舱基准价或要求补齐总裁特批"
            )
        elif hotel_over > 0 and meal_over > 0:
            verdict = "rejected_double_over_limit"
            verdict_label = "双项超标拦截"
            action = f"核减住宿与餐补合计超标 {total_over:.2f} 元，退回修改或自费承担超标部分"
        elif total_over > 0 and advance_overdue:
            verdict = "rejected_over_limit_and_advance_overdue"
            verdict_label = "超标且备用金逾期双拦截"
            action = (
                f"核减超标金额 {total_over:.2f} 元，并同步冲抵逾期备用金 "
                f"{advance_id}（{advance_amount:,.2f} 元），余款限期 3 个工作日内退回"
            )
        elif total_over > 0:
            verdict = "rejected_over_limit"
            verdict_label = "超标拦截"
            action = f"核减超标金额 {total_over:.2f} 元（核对电子发票后按标准限额放行）"
        elif advance_overdue:
            verdict = "advance_overdue_warning"
            verdict_label = "备用金超期预警"
            action = f"先以本单冲抵未核销备用金 {advance_id}，余款限期 3 个工作日内退回"
        else:
            verdict = "approved_compliant"
            verdict_label = "合规放行"
            action = "各项均符合 XC-FIN-2026-006 标准，建议财务审核放行"

        audited_items.append(
            AuditedExpense(
                expense_id=expense_id,
                employee_no=emp_no,
                employee_name=emp_name,
                department_id=dept_id,
                department_name=dept_name,
                employment_status=emp_status,
                destination_city=city,
                city_tier=city_tier,
                hotel_nights=hotel_nights,
                hotel_rate_per_night=hotel_rate,
                hotel_limit_per_night=hotel_limit,
                hotel_over_cny=hotel_over,
                meal_days=meal_days,
                meal_per_day=meal_per_day,
                meal_limit_per_day=meal_limit,
                meal_over_cny=meal_over,
                total_over_cny=total_over,
                transport_cabin=cabin,
                transport_amount_cny=transport_amount,
                cabin_violation=cabin_violation,
                advance_id=advance_id,
                advance_amount_cny=advance_amount,
                advance_overdue_or_offboarding=advance_alert,
                workdays_since_return=workdays_since_return,
                total_claimed_cny=total_claimed,
                verdict=verdict,
                verdict_label=verdict_label,
                violation_reasons=tuple(reasons),
                recommended_action=action,
            )
        )

    compliant = tuple(item for item in audited_items if item.verdict == "approved_compliant")
    violations = tuple(
        item for item in audited_items if item.total_over_cny > 0 or item.cabin_violation
    )
    advance_alerts = tuple(item for item in audited_items if item.advance_overdue_or_offboarding)

    return TravelAuditReport(
        month=month,
        baseline_date=baseline_date,
        department_id=target_dept,
        department_label=department_label,
        total_expenses=len(audited_items),
        total_claimed_cny=round(sum(item.total_claimed_cny for item in audited_items), 2),
        total_over_limit_cny=round(sum(item.total_over_cny for item in audited_items), 2),
        total_unreconciled_advance_cny=round(
            sum(item.advance_amount_cny for item in advance_alerts), 2
        ),
        compliant_items=compliant,
        violation_items=violations,
        advance_alert_items=advance_alerts,
        all_items=tuple(audited_items),
    )


def _style_sheet(sheet: Any, widths: Sequence[int]) -> None:
    """统一设置深蓝表头、冻结首行、自动筛选与列宽。"""
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


def write_travel_audit_workbook(report: TravelAuditReport, output_directory: Path) -> Path:
    """生成四工作表差旅超标与备用金稽核 Excel。"""
    output_directory.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    # Sheet 1：审计总览
    overview = workbook.active
    overview.title = "审计总览"
    overview.append(["稽核指标", "数值/范围", "说明"])
    overview.append(["稽核月份", report.month, f"基准日期 {report.baseline_date}"])
    overview.append(["部门范围", report.department_label, report.department_id])
    overview.append(["报销单总笔数", report.total_expenses, "覆盖范围内全部待审/已审差旅单"])
    overview.append(["申报总金额(元)", report.total_claimed_cny, "人民币两位小数口径"])
    overview.append(["合规放行笔数", len(report.compliant_items), "住宿、餐补、舱位及备用金均合规"])
    overview.append(
        ["超标与舱位违规笔数", len(report.violation_items), "需财务核减超标额或驳回违规舱位"]
    )
    overview.append(
        ["住宿与餐补合计超标金额(元)", report.total_over_limit_cny, "不含违规舱位机票全额"]
    )
    overview.append(
        ["备用金逾期/离职阻断笔数", len(report.advance_alert_items), "返程超 15 工作日或离职红线"]
    )
    overview.append(
        [
            "未核销高风险备用金总额(元)",
            report.total_unreconciled_advance_cny,
            "离职员工须于最后工作日 T-5 前清零",
        ]
    )
    _style_sheet(overview, (28, 24, 54))

    # Sheet 2：超标与违规明细
    viol_sheet = workbook.create_sheet("超标与违规明细")
    viol_sheet.append(
        [
            "报销单号",
            "员工工号",
            "姓名",
            "部门",
            "出差城市",
            "城市等级",
            "住宿实报(元/晚)",
            "住宿上限(元/晚)",
            "住宿超标(元)",
            "餐补实报(元/天)",
            "餐补上限(元/天)",
            "餐补超标(元)",
            "合计超标(元)",
            "交通舱位",
            "交通金额(元)",
            "稽核判定",
            "违规明细",
            "财务处置建议",
        ]
    )
    for item in report.violation_items:
        viol_sheet.append(
            [
                item.expense_id,
                item.employee_no,
                item.employee_name,
                item.department_name,
                item.destination_city,
                item.city_tier,
                item.hotel_rate_per_night,
                item.hotel_limit_per_night,
                item.hotel_over_cny,
                item.meal_per_day,
                item.meal_limit_per_day,
                item.meal_over_cny,
                item.total_over_cny,
                item.transport_cabin,
                item.transport_amount_cny,
                item.verdict_label,
                "；".join(item.violation_reasons),
                item.recommended_action,
            ]
        )
    _style_sheet(
        viol_sheet,
        (16, 12, 10, 14, 12, 10, 14, 14, 14, 14, 14, 14, 14, 16, 14, 16, 52, 46),
    )

    # Sheet 3：合规放行清单
    pass_sheet = workbook.create_sheet("合规放行清单")
    pass_sheet.append(
        [
            "报销单号",
            "员工工号",
            "姓名",
            "部门",
            "出差城市",
            "住宿晚数",
            "住宿单价(元/晚)",
            "餐补单价(元/天)",
            "交通舱位",
            "申报总额(元)",
            "稽核判定",
        ]
    )
    for item in report.compliant_items:
        pass_sheet.append(
            [
                item.expense_id,
                item.employee_no,
                item.employee_name,
                item.department_name,
                item.destination_city,
                item.hotel_nights,
                item.hotel_rate_per_night,
                item.meal_per_day,
                item.transport_cabin,
                item.total_claimed_cny,
                item.verdict_label,
            ]
        )
    _style_sheet(pass_sheet, (16, 12, 10, 14, 12, 10, 14, 14, 16, 14, 14))

    # Sheet 4：备用金逾期预警
    adv_sheet = workbook.create_sheet("备用金逾期预警")
    adv_sheet.append(
        [
            "报销单号",
            "备用金单号",
            "员工工号",
            "姓名",
            "部门",
            "档案状态",
            "未核销金额(元)",
            "返程已过工作日",
            "预警结论",
            "处置建议",
        ]
    )
    for item in report.advance_alert_items:
        adv_sheet.append(
            [
                item.expense_id,
                item.advance_id or "-",
                item.employee_no,
                item.employee_name,
                item.department_name,
                item.employment_status,
                item.advance_amount_cny,
                item.workdays_since_return,
                item.verdict_label,
                item.recommended_action,
            ]
        )
    _style_sheet(adv_sheet, (16, 20, 12, 10, 14, 14, 16, 16, 20, 52))

    output_path = output_directory / f"travel-audit-{report.month}.xlsx"
    workbook.save(output_path)
    return output_path


def write_travel_audit_summary(report: TravelAuditReport, output_directory: Path) -> Path:
    """生成可供 session_file_read 完整读取的 UTF-8 差旅稽核风险摘要文本。"""
    output_directory.mkdir(parents=True, exist_ok=True)
    lines = [
        f"星辰智联 {report.month} 差旅报销超标与备用金稽核摘要",
        f"稽核范围：{report.department_label}",
        f"基准日期：{report.baseline_date}",
        (
            f"总体情况：共核验 {report.total_expenses} 笔差旅报销单，申报总额 "
            f"{report.total_claimed_cny:,.2f} 元；合规放行 {len(report.compliant_items)} 笔，"
            f"超标/舱位违规 {len(report.violation_items)} 笔（住宿/餐补合计超标 "
            f"{report.total_over_limit_cny:,.2f} 元），备用金逾期/离职阻断预警 "
            f"{len(report.advance_alert_items)} 笔（未核销金额 "
            f"{report.total_unreconciled_advance_cny:,.2f} 元）。"
        ),
        "",
        "一、超标与舱位违规拦截清单：",
    ]
    if report.violation_items:
        for item in report.violation_items:
            lines.append(
                f"- [{item.verdict_label}] {item.expense_id} {item.employee_no} "
                f"{item.employee_name}（{item.department_name}，{item.destination_city}）："
                f"{'；'.join(item.violation_reasons)} | 建议：{item.recommended_action}"
            )
    else:
        lines.append("- 无超标或舱位违规单据")

    lines.append("")
    lines.append("二、备用金逾期与离职阻断预警清单：")
    if report.advance_alert_items:
        for item in report.advance_alert_items:
            lines.append(
                f"- [{item.verdict_label}] {item.expense_id}（备用金 {item.advance_id}，"
                f"{item.advance_amount_cny:,.2f} 元）{item.employee_no} {item.employee_name}"
                f"（{item.department_name}，状态 {item.employment_status}）："
                f"{'；'.join(item.violation_reasons)} | 建议：{item.recommended_action}"
            )
    else:
        lines.append("- 无备用金逾期或离职阻断预警")

    lines.append("")
    lines.append("三、合规放行清单：")
    if report.compliant_items:
        for item in report.compliant_items:
            lines.append(
                f"- [合规放行] {item.expense_id} {item.employee_no} {item.employee_name}"
                f"（{item.department_name}，{item.destination_city}）：申报总额 "
                f"{item.total_claimed_cny:,.2f} 元，全部符合 XC-FIN-2026-006 标准"
            )
    else:
        lines.append("- 无合规放行单据")

    summary_path = output_directory / f"travel-audit-{report.month}-summary.txt"
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
        raise TravelAuditInputError("output_dir_invalid")
    return parameters, Path(output_dir)


def main() -> None:
    """Athena Skill 入口：生成四表差旅稽核 Excel、UTF-8 风险摘要及 manifest.json。"""
    try:
        parameters, output_directory = _load_request()
        month = _required_text(parameters, "month", "parameters")
        department_id = _required_text(parameters, "department_id", "parameters")
        travel_inputs = _read_object(parameters.get("travel_inputs"), "parameters.travel_inputs")
        report = build_travel_audit_report(travel_inputs, department_id)
        if report.month != month:
            raise TravelAuditInputError("parameters.month_mismatch")

        workbook_path = write_travel_audit_workbook(report, output_directory)
        summary_path = write_travel_audit_summary(report, output_directory)
        manifest_path = output_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "outputs": [
                        {
                            "path": workbook_path.name,
                            "label": (
                                f"{report.month} {report.department_id} 差旅超标与备用金稽核台账"
                            ),
                        },
                        {
                            "path": summary_path.name,
                            "label": (
                                f"{report.month} {report.department_id} 差旅超标与备用金风险摘要"
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
                    "total_expenses": report.total_expenses,
                    "compliant_count": len(report.compliant_items),
                    "violation_count": len(report.violation_items),
                    "advance_alert_count": len(report.advance_alert_items),
                    "total_over_limit_cny": report.total_over_limit_cny,
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
        TravelAuditInputError,
        ValueError,
    ) as error:
        raise SystemExit(f"travel_audit_skill_failed:{error}") from error


if __name__ == "__main__":
    main()
