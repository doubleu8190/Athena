#!/usr/bin/env python3
"""按部门自然日在职区间生成月度薪资 Excel。"""

from __future__ import annotations

import calendar
import json
import os
import re
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]

MONEY_QUANTUM = Decimal("0.01")
DAILY_DIVISOR = Decimal("21.75")
MONTH_PATTERN = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")

type JsonObject = dict[str, object]


class PayrollInputError(ValueError):
    """表示薪资聚合输入不符合 Skill 契约。"""


@dataclass(frozen=True, slots=True)
class Employee:
    """一名参与月度核算的员工。"""

    employee_id: str
    name: str
    hire_date: date | None


@dataclass(frozen=True, slots=True)
class DepartmentInterval:
    """员工在当月内的一段部门任职自然日区间。"""

    department_id: str
    department_name: str
    start_date: date
    end_date: date

    @property
    def natural_days(self) -> int:
        """返回该区间包含的自然日数量。"""
        return (self.end_date - self.start_date).days + 1


@dataclass(frozen=True, slots=True)
class UnpaidLeave:
    """一条按日期归属部门的无薪假记录。"""

    leave_date: date
    days: Decimal


@dataclass(frozen=True, slots=True)
class Bonus:
    """一条部门奖金记录。"""

    department_id: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Termination:
    """员工的离职日期和最终部门。"""

    termination_date: date
    department_id: str


@dataclass(frozen=True, slots=True)
class PayrollLine:
    """一名员工在一个部门内的完整薪资明细。"""

    employee_id: str
    employee_name: str
    department_id: str
    department_name: str
    employed_days: int
    month_days: int
    monthly_base_salary: Decimal
    base_pay: Decimal
    unpaid_leave_days: Decimal
    unpaid_leave_deduction: Decimal
    bonus: Decimal
    annual_leave_days: Decimal
    annual_leave_settlement: Decimal
    payable_salary: Decimal
    termination_date: date | None


@dataclass(frozen=True, slots=True)
class FailedEmployee:
    """一名因数据不完整而未进入总额的员工。"""

    employee_id: str
    employee_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class DepartmentSummary:
    """一个成功核算部门的薪资汇总。"""

    department_id: str
    department_name: str
    employee_count: int
    natural_days: int
    base_pay: Decimal
    unpaid_leave_deduction: Decimal
    bonus: Decimal
    annual_leave_settlement: Decimal
    payable_salary: Decimal


@dataclass(frozen=True, slots=True)
class PayrollReport:
    """一次月度核算的全部成功明细、部门汇总和失败记录。"""

    month: str
    department_id: str
    month_days: int
    daily_divisor: Decimal
    lines: tuple[PayrollLine, ...]
    department_summaries: tuple[DepartmentSummary, ...]
    failed_employees: tuple[FailedEmployee, ...]
    total_payable_salary: Decimal


@dataclass(slots=True)
class _DepartmentAccumulator:
    """在构建部门汇总时累计同一部门的金额。"""

    department_name: str
    employee_ids: set[str]
    natural_days: int = 0
    base_pay: Decimal = Decimal("0")
    unpaid_leave_deduction: Decimal = Decimal("0")
    bonus: Decimal = Decimal("0")
    annual_leave_settlement: Decimal = Decimal("0")
    payable_salary: Decimal = Decimal("0")


def _read_object(value: object, location: str) -> JsonObject:
    """把 JSON 对象边界收窄为字符串键字典。

    参数:
        value: JSON 解码得到的候选对象。
        location: 用于错误提示的字段路径。

    返回:
        字符串键 JSON 对象。

    异常:
        值不是字符串键对象时抛出 ``PayrollInputError``。
    """
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PayrollInputError(f"{location}_invalid")
    return cast(JsonObject, value)


def _records(payload: JsonObject, key: str) -> Iterator[JsonObject]:
    """逐个读取并校验七类记录数组。

    参数:
        payload: 薪资聚合对象。
        key: 需要读取的记录数组字段。

    返回:
        每次产出一个字符串键记录对象。

    异常:
        字段不是对象数组时抛出 ``PayrollInputError``。
    """
    value = payload.get(key)
    if not isinstance(value, list):
        raise PayrollInputError(f"{key}_invalid")
    for index, item in enumerate(value):
        yield _read_object(item, f"{key}[{index}]")


def _required_text(record: JsonObject, key: str, location: str) -> str:
    """读取必填非空字符串。

    参数:
        record: 当前 JSON 对象。
        key: 字段名。
        location: 用于错误提示的对象路径。

    返回:
        去除首尾空白后的文本。

    异常:
        字段缺失、类型错误或为空时抛出 ``PayrollInputError``。
    """
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PayrollInputError(f"{location}.{key}_invalid")
    return value.strip()


def _required_int(record: JsonObject, key: str, location: str) -> int:
    """读取必填整数并拒绝布尔值。

    参数:
        record: 当前 JSON 对象。
        key: 字段名。
        location: 用于错误提示的对象路径。

    返回:
        合法整数。

    异常:
        字段不是整数或实际为布尔值时抛出 ``PayrollInputError``。
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayrollInputError(f"{location}.{key}_invalid")
    return value


def _required_decimal(record: JsonObject, key: str, location: str) -> Decimal:
    """把 JSON 数值字段转换为十进制数。

    参数:
        record: 当前 JSON 对象。
        key: 字段名。
        location: 用于错误提示的对象路径。

    返回:
        可继续参与金额或天数计算的 ``Decimal``。

    异常:
        字段缺失、为布尔值或无法解析为有限数字时抛出 ``PayrollInputError``。
    """
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PayrollInputError(f"{location}.{key}_invalid")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise PayrollInputError(f"{location}.{key}_invalid") from error
    if not parsed.is_finite():
        raise PayrollInputError(f"{location}.{key}_invalid")
    return parsed


def _required_date(record: JsonObject, key: str, location: str) -> date:
    """读取 ISO ``YYYY-MM-DD`` 日期。

    参数:
        record: 当前 JSON 对象。
        key: 字段名。
        location: 用于错误提示的对象路径。

    返回:
        解析后的日期。

    异常:
        字段不是合法 ISO 日期时抛出 ``PayrollInputError``。
    """
    value = _required_text(record, key, location)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise PayrollInputError(f"{location}.{key}_invalid") from error


def _optional_date(record: JsonObject, key: str, location: str) -> date | None:
    """读取可空 ISO 日期。

    参数:
        record: 当前 JSON 对象。
        key: 字段名。
        location: 用于错误提示的对象路径。

    返回:
        日期值；字段显式为 ``null`` 时返回 ``None``。

    异常:
        字段既不是 ``null`` 也不是合法 ISO 日期时抛出 ``PayrollInputError``。
    """
    value = record.get(key)
    if value is None:
        return None
    return _required_date(record, key, location)


def _month_bounds(month: str) -> tuple[date, date, int]:
    """解析月份并返回首日、末日和自然日数。

    参数:
        month: 严格格式的 ``YYYY-MM`` 月份。

    返回:
        当月首日、当月末日和当月自然日数。

    异常:
        月份格式或日历范围非法时抛出 ``PayrollInputError``。
    """
    matched = MONTH_PATTERN.fullmatch(month)
    if matched is None:
        raise PayrollInputError("month_invalid")
    year = int(matched.group(1))
    month_number = int(matched.group(2))
    try:
        month_days = calendar.monthrange(year, month_number)[1]
    except (ValueError, OverflowError) as error:
        raise PayrollInputError("month_invalid") from error
    return date(year, month_number, 1), date(year, month_number, month_days), month_days


def _money(value: Decimal) -> Decimal:
    """按人民币分位执行四舍五入。

    参数:
        value: 尚未舍入的金额。

    返回:
        两位小数的金额。

    异常:
        本函数不主动抛出业务异常。
    """
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _group_records(
    records: Sequence[JsonObject],
    employee_ids: set[str],
    location: str,
) -> dict[str, list[JsonObject]]:
    """按员工编号聚合记录，并拒绝未知员工。

    参数:
        records: 同一类型的上游记录。
        employee_ids: 员工目录中已知的编号集合。
        location: 用于错误提示的记录类型。

    返回:
        每名已知员工对应的原始记录列表。

    异常:
        记录缺少员工编号或引用未知员工时抛出 ``PayrollInputError``。
    """
    grouped: dict[str, list[JsonObject]] = {employee_id: [] for employee_id in employee_ids}
    for index, record in enumerate(records):
        employee_id = _required_text(record, "employee_id", f"{location}[{index}]")
        if employee_id not in employee_ids:
            raise PayrollInputError(f"{location}[{index}].employee_id_unknown")
        grouped[employee_id].append(record)
    return grouped


def _parse_employees(payload: JsonObject) -> dict[str, Employee]:
    """读取员工目录并确保员工编号唯一。

    参数:
        payload: 薪资聚合对象。

    返回:
        员工编号到员工实体的字典。

    异常:
        员工记录重复或字段非法时抛出 ``PayrollInputError``。
    """
    employees: dict[str, Employee] = {}
    for index, record in enumerate(_records(payload, "employees")):
        location = f"employees[{index}]"
        employee_id = _required_text(record, "employee_id", location)
        name = _required_text(record, "name", location)
        if employee_id in employees:
            raise PayrollInputError(f"employees[{index}].employee_id_duplicate")
        employees[employee_id] = Employee(
            employee_id,
            name,
            hire_date=_optional_date(record, "hire_date", location),
        )
    return employees


def _parse_assignment(record: JsonObject, location: str) -> DepartmentInterval:
    """读取一条完整部门任职记录。

    参数:
        record: 部门任职记录。
        location: 用于错误提示的对象路径。

    返回:
        未裁剪到目标月份和离职日期的任职区间。

    异常:
        日期区间非法时抛出 ``PayrollInputError``。
    """
    start_date = _required_date(record, "start_date", location)
    end_date = _optional_date(record, "end_date", location)
    if end_date is not None and end_date < start_date:
        raise PayrollInputError(f"{location}.date_range_invalid")
    return DepartmentInterval(
        department_id=_required_text(record, "department_id", location),
        department_name=_required_text(record, "department_name", location),
        start_date=start_date,
        end_date=end_date or date.max,
    )


def _build_intervals(
    records: Sequence[JsonObject],
    *,
    month_start: date,
    month_end: date,
    hire_date: date | None,
    termination_date: date | None,
    location: str,
) -> tuple[DepartmentInterval, ...]:
    """裁剪任职区间并确认目标期间没有重叠或空档。

    参数:
        records: 员工部门任职记录。
        month_start: 核算月份首日。
        month_end: 核算月份末日。
        hire_date: 可证明的入职日期；未知时为 ``None``。
        termination_date: 离职日期；未离职时为 ``None``。
        location: 用于错误提示的记录类型。

    返回:
        按日期排序且连续覆盖在职期间的部门区间。

    异常:
        缺少任职记录、区间重叠或存在未覆盖自然日时抛出 ``PayrollInputError``。
    """
    if not records:
        raise PayrollInputError(f"{location}_missing")
    active_end = min(month_end, termination_date) if termination_date is not None else month_end
    if active_end < month_start:
        raise PayrollInputError("termination_date_outside_month")

    parsed = sorted(
        (_parse_assignment(record, f"{location}[{index}]") for index, record in enumerate(records)),
        key=lambda item: (item.start_date, item.end_date),
    )
    # 未提供入职日期时，以最早有效任职记录作为可证明的在职起点，避免把月中入职误判为空档。
    employment_start = max(month_start, hire_date or parsed[0].start_date)
    if active_end < employment_start:
        raise PayrollInputError("employment_period_invalid")
    clipped: list[DepartmentInterval] = []
    cursor = employment_start
    for interval in parsed:
        start_date = max(interval.start_date, employment_start)
        end_date = min(interval.end_date, active_end)
        if end_date < start_date:
            continue
        if start_date > cursor:
            raise PayrollInputError("department_assignment_gap")
        if start_date < cursor:
            raise PayrollInputError("department_assignment_overlap")
        clipped.append(
            DepartmentInterval(
                department_id=interval.department_id,
                department_name=interval.department_name,
                start_date=start_date,
                end_date=end_date,
            )
        )
        cursor = end_date + timedelta(days=1)
    if cursor != active_end + timedelta(days=1):
        raise PayrollInputError("department_assignment_gap")
    return tuple(clipped)


def _interval_index_for_date(
    intervals: Sequence[DepartmentInterval],
    target_date: date,
) -> int | None:
    """返回包含指定自然日的部门区间序号。

    参数:
        intervals: 已连续覆盖在职期间的部门区间。
        target_date: 待归属日期。

    返回:
        匹配区间的下标；目标日期不在任何区间时返回 ``None``。

    异常:
        本函数不主动抛出业务异常。
    """
    return next(
        (
            index
            for index, interval in enumerate(intervals)
            if interval.start_date <= target_date <= interval.end_date
        ),
        None,
    )


def _parse_unpaid_leave(record: JsonObject, location: str) -> UnpaidLeave:
    """读取一条无薪假记录。

    参数:
        record: 无薪假记录。
        location: 用于错误提示的对象路径。

    返回:
        日期和正数休假天数。

    异常:
        休假天数不为正数时抛出 ``PayrollInputError``。
    """
    days = _required_decimal(record, "days", location)
    if days <= 0:
        raise PayrollInputError(f"{location}.days_invalid")
    return UnpaidLeave(_required_date(record, "leave_date", location), days)


def _parse_bonus(record: JsonObject, location: str) -> Bonus:
    """读取一条部门奖金记录。

    参数:
        record: 奖金记录。
        location: 用于错误提示的对象路径。

    返回:
        部门编号和非负奖金金额。

    异常:
        奖金为负数时抛出 ``PayrollInputError``。
    """
    amount = _required_decimal(record, "amount", location)
    if amount < 0:
        raise PayrollInputError(f"{location}.amount_invalid")
    return Bonus(_required_text(record, "department_id", location), amount)


def _parse_termination(record: JsonObject, location: str) -> Termination:
    """读取一条离职记录。

    参数:
        record: 离职记录。
        location: 用于错误提示的对象路径。

    返回:
        离职日期和最终部门编号。

    异常:
        离职日期或部门字段非法时抛出 ``PayrollInputError``。
    """
    return Termination(
        termination_date=_required_date(record, "termination_date", location),
        department_id=_required_text(record, "termination_department_id", location),
    )


def _department_summaries(lines: Sequence[PayrollLine]) -> tuple[DepartmentSummary, ...]:
    """按部门累计成功员工的自然日与薪资组成。

    参数:
        lines: 已成功完成核算的部门明细。

    返回:
        按部门编号排序的汇总记录。

    异常:
        本函数不主动抛出业务异常。
    """
    accumulators: dict[str, _DepartmentAccumulator] = {}
    for line in lines:
        accumulator = accumulators.setdefault(
            line.department_id,
            _DepartmentAccumulator(
                department_name=line.department_name,
                employee_ids=set(),
            ),
        )
        accumulator.employee_ids.add(line.employee_id)
        accumulator.natural_days += line.employed_days
        accumulator.base_pay += line.base_pay
        accumulator.unpaid_leave_deduction += line.unpaid_leave_deduction
        accumulator.bonus += line.bonus
        accumulator.annual_leave_settlement += line.annual_leave_settlement
        accumulator.payable_salary += line.payable_salary
    return tuple(
        DepartmentSummary(
            department_id=department_id,
            department_name=accumulator.department_name,
            employee_count=len(accumulator.employee_ids),
            natural_days=accumulator.natural_days,
            base_pay=_money(accumulator.base_pay),
            unpaid_leave_deduction=_money(accumulator.unpaid_leave_deduction),
            bonus=_money(accumulator.bonus),
            annual_leave_settlement=_money(accumulator.annual_leave_settlement),
            payable_salary=_money(accumulator.payable_salary),
        )
        for department_id, accumulator in sorted(accumulators.items())
    )


def build_payroll_report(payload: JsonObject, department_id: str) -> PayrollReport:
    """核算指定月份和目标部门的薪资明细、失败员工及部门汇总。

    参数:
        payload: ``payroll_query`` 返回的完整结构化内容。
        department_id: 本次需要核算的部门编号。

    返回:
        仅包含目标部门员工和明细的可写入核算结果。

    异常:
        月份、基础结构、目标部门不存在或全局员工引用非法时抛出 ``PayrollInputError``；
        单名员工的数据问题只进入失败列表。
    """
    target_department_id = department_id.strip()
    if not target_department_id:
        raise PayrollInputError("department_id_invalid")
    schema_version = _required_int(payload, "schema_version", "payroll_inputs")
    if schema_version != 1:
        raise PayrollInputError("schema_version_unsupported")
    month = _required_text(payload, "month", "payroll_inputs")
    month_start, month_end, month_days = _month_bounds(month)
    supplied_month_days = _required_int(payload, "month_days", "payroll_inputs")
    if supplied_month_days != month_days:
        raise PayrollInputError("month_days_mismatch")
    daily_divisor = _required_decimal(payload, "daily_divisor", "payroll_inputs")
    if daily_divisor != DAILY_DIVISOR:
        raise PayrollInputError("daily_divisor_invalid")

    employees = _parse_employees(payload)
    employee_ids = set(employees)
    assignments_by_employee = _group_records(
        tuple(_records(payload, "department_assignments")),
        employee_ids,
        "department_assignments",
    )
    target_employee_ids: set[str] = set()
    for employee_id, records in assignments_by_employee.items():
        for record in records:
            record_department_id = record.get("department_id")
            raw_matches_target = (
                isinstance(record_department_id, str)
                and record_department_id.strip() == target_department_id
            )
            try:
                assignment = _parse_assignment(record, "department_assignments")
            except PayrollInputError:
                # 日期损坏的目标部门记录仍需纳入失败明细，不能被“未找到部门”掩盖。
                if raw_matches_target:
                    target_employee_ids.add(employee_id)
                continue
            if (
                assignment.department_id == target_department_id
                and assignment.start_date <= month_end
                and assignment.end_date >= month_start
            ):
                target_employee_ids.add(employee_id)
                break
    if not target_employee_ids:
        raise PayrollInputError("department_not_found")
    salaries_by_employee = _group_records(
        tuple(_records(payload, "base_salaries")),
        employee_ids,
        "base_salaries",
    )
    leaves_by_employee = _group_records(
        tuple(_records(payload, "unpaid_leaves")),
        employee_ids,
        "unpaid_leaves",
    )
    bonuses_by_employee = _group_records(
        tuple(_records(payload, "bonuses")),
        employee_ids,
        "bonuses",
    )
    balances_by_employee = _group_records(
        tuple(_records(payload, "annual_leave_balances")),
        employee_ids,
        "annual_leave_balances",
    )
    terminations_by_employee = _group_records(
        tuple(_records(payload, "terminations")),
        employee_ids,
        "terminations",
    )

    lines: list[PayrollLine] = []
    failures: list[FailedEmployee] = []
    for employee in employees.values():
        if employee.employee_id not in target_employee_ids:
            continue
        reasons: list[str] = []
        termination: Termination | None = None
        termination_records = terminations_by_employee[employee.employee_id]
        if len(termination_records) > 1:
            reasons.append("离职记录重复")
        elif termination_records:
            try:
                termination = _parse_termination(
                    termination_records[0],
                    "terminations",
                )
                if not month_start <= termination.termination_date <= month_end:
                    reasons.append("离职日期不在核算月份")
            except PayrollInputError as error:
                reasons.append(str(error))

        salary_records = salaries_by_employee[employee.employee_id]
        monthly_salary: Decimal | None = None
        if not salary_records:
            reasons.append("缺少月基本工资")
        elif len(salary_records) > 1:
            reasons.append("月基本工资记录重复")
        else:
            try:
                monthly_salary = _required_decimal(
                    salary_records[0],
                    "monthly_base_salary",
                    "base_salaries",
                )
                if monthly_salary < 0:
                    reasons.append("月基本工资为负数")
            except PayrollInputError as error:
                reasons.append(str(error))

        try:
            intervals = _build_intervals(
                assignments_by_employee[employee.employee_id],
                month_start=month_start,
                month_end=month_end,
                hire_date=employee.hire_date,
                termination_date=(
                    termination.termination_date if termination is not None else None
                ),
                location="department_assignments",
            )
        except PayrollInputError as error:
            reasons.append(str(error))
            intervals = ()

        termination_interval_matches = False
        if termination is not None:
            termination_interval_index = _interval_index_for_date(
                intervals,
                termination.termination_date,
            )
            termination_interval_matches = (
                termination_interval_index is not None
                and intervals[termination_interval_index].department_id == termination.department_id
            )
            if not termination_interval_matches:
                reasons.append("离职最终部门与离职日期任职区间不匹配")
        target_intervals = tuple(
            (interval_index, interval)
            for interval_index, interval in enumerate(intervals)
            if interval.department_id == target_department_id
        )
        if not target_intervals:
            reasons.append("目标部门在核算月份无任职区间")

        leave_days_by_interval: dict[int, Decimal] = defaultdict(Decimal)
        for index, record in enumerate(leaves_by_employee[employee.employee_id]):
            try:
                leave = _parse_unpaid_leave(record, f"unpaid_leaves[{index}]")
                if not month_start <= leave.leave_date <= month_end:
                    reasons.append("无薪假日期不在核算月份")
                    continue
                if termination is not None and leave.leave_date > termination.termination_date:
                    reasons.append("无薪假日期晚于离职日期")
                    continue
                interval_index = _interval_index_for_date(intervals, leave.leave_date)
                if interval_index is None:
                    reasons.append("无薪假日期不在任职区间")
                    continue
                if intervals[interval_index].department_id == target_department_id:
                    leave_days_by_interval[interval_index] += leave.days
            except PayrollInputError as error:
                reasons.append(str(error))

        for interval_index, interval in target_intervals:
            if leave_days_by_interval[interval_index] > Decimal(interval.natural_days):
                reasons.append("无薪假天数超过部门在职自然日")
                break

        bonus_amount_by_department: dict[str, Decimal] = defaultdict(Decimal)
        for index, record in enumerate(bonuses_by_employee[employee.employee_id]):
            try:
                parsed_bonus = _parse_bonus(record, f"bonuses[{index}]")
                if parsed_bonus.department_id != target_department_id:
                    continue
                bonus_amount_by_department[parsed_bonus.department_id] += parsed_bonus.amount
            except PayrollInputError as error:
                reasons.append(str(error))

        annual_leave_days = Decimal("0")
        balance_records = balances_by_employee[employee.employee_id]
        termination_in_target_department = (
            termination_interval_matches
            and termination is not None
            and termination.department_id == target_department_id
        )
        if termination_in_target_department:
            if not balance_records:
                reasons.append("离职员工缺少年假余额")
            elif len(balance_records) > 1:
                reasons.append("年假余额记录重复")
            else:
                try:
                    annual_leave_days = _required_decimal(
                        balance_records[0],
                        "annual_leave_days",
                        "annual_leave_balances",
                    )
                except PayrollInputError as error:
                    reasons.append(str(error))

        if reasons or monthly_salary is None:
            failures.append(
                FailedEmployee(
                    employee_id=employee.employee_id,
                    employee_name=employee.name,
                    reason="；".join(dict.fromkeys(reasons)),
                )
            )
            continue

        daily_wage = monthly_salary / daily_divisor
        # 目标部门可能重复任职：奖金只归首个区间，离职年假结算只归最后一个区间。
        first_target_interval_index = target_intervals[0][0]
        last_target_interval_index = target_intervals[-1][0]
        for interval_index, interval in target_intervals:
            base_pay = _money(monthly_salary * Decimal(interval.natural_days) / Decimal(month_days))
            unpaid_leave_days = leave_days_by_interval[interval_index]
            unpaid_leave_deduction = _money(unpaid_leave_days * daily_wage)
            bonus = Decimal("0")
            if first_target_interval_index == interval_index:
                bonus = _money(bonus_amount_by_department[interval.department_id])
            settles_annual_leave = (
                termination_in_target_department and last_target_interval_index == interval_index
            )
            annual_leave_settlement = Decimal("0")
            if settles_annual_leave:
                annual_leave_settlement = _money(annual_leave_days * daily_wage)
            payable_salary = _money(
                base_pay - unpaid_leave_deduction + bonus + annual_leave_settlement
            )
            lines.append(
                PayrollLine(
                    employee_id=employee.employee_id,
                    employee_name=employee.name,
                    department_id=interval.department_id,
                    department_name=interval.department_name,
                    employed_days=interval.natural_days,
                    month_days=month_days,
                    monthly_base_salary=_money(monthly_salary),
                    base_pay=base_pay,
                    unpaid_leave_days=unpaid_leave_days,
                    unpaid_leave_deduction=unpaid_leave_deduction,
                    bonus=bonus,
                    annual_leave_days=annual_leave_days if settles_annual_leave else Decimal("0"),
                    annual_leave_settlement=annual_leave_settlement,
                    payable_salary=payable_salary,
                    termination_date=(
                        termination.termination_date if termination is not None else None
                    ),
                )
            )

    return PayrollReport(
        month=month,
        department_id=target_department_id,
        month_days=month_days,
        daily_divisor=daily_divisor,
        lines=tuple(lines),
        department_summaries=_department_summaries(lines),
        failed_employees=tuple(failures),
        total_payable_salary=_money(sum((line.payable_salary for line in lines), Decimal("0"))),
    )


def _style_sheet(sheet: Any, widths: Sequence[int]) -> None:
    """设置表头、冻结窗格、筛选和列宽。

    参数:
        sheet: openpyxl 工作表。
        widths: 从第一列开始的列宽。

    返回:
        无返回值。

    异常:
        工作表结构不合法时传播 openpyxl 异常。
    """
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width


def _number(value: Decimal) -> float:
    """把 ``Decimal`` 转为 Excel 可写入的数值。

    参数:
        value: 已完成分位舍入的金额或天数。

    返回:
        Excel 使用的浮点数。

    异常:
        本函数不主动抛出业务异常。
    """
    return float(value)


def write_excel(report: PayrollReport, output_directory: Path) -> Path:
    """生成员工、部门、明细、失败和规则五张工作表。

    参数:
        report: 已完成的薪资核算结果。
        output_directory: Athena 为本次 Skill 调用创建的输出目录。

    返回:
        已写入的 Excel 绝对路径。

    异常:
        输出目录或工作簿无法写入时传播底层 ``OSError``。
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    employee_sheet = workbook.active
    employee_sheet.title = "员工汇总"
    employee_sheet.append(
        [
            "月份",
            "员工编号",
            "姓名",
            "在职自然日",
            "月自然日",
            "月基本工资",
            "基本工资",
            "无薪假扣款",
            "奖金",
            "年假结算",
            "应付薪资",
        ]
    )
    lines_by_employee: dict[str, list[PayrollLine]] = defaultdict(list)
    for line in report.lines:
        lines_by_employee[line.employee_id].append(line)
    for employee_id, employee_lines in sorted(lines_by_employee.items()):
        first = employee_lines[0]
        employee_sheet.append(
            [
                report.month,
                employee_id,
                first.employee_name,
                sum(line.employed_days for line in employee_lines),
                report.month_days,
                _number(first.monthly_base_salary),
                _number(sum((line.base_pay for line in employee_lines), Decimal("0"))),
                _number(
                    sum(
                        (line.unpaid_leave_deduction for line in employee_lines),
                        Decimal("0"),
                    )
                ),
                _number(sum((line.bonus for line in employee_lines), Decimal("0"))),
                _number(
                    sum(
                        (line.annual_leave_settlement for line in employee_lines),
                        Decimal("0"),
                    )
                ),
                _number(sum((line.payable_salary for line in employee_lines), Decimal("0"))),
            ]
        )
    employee_sheet.append(
        ["", "合计", "", "", "", "", "", "", "", "", _number(report.total_payable_salary)]
    )
    for row in employee_sheet.iter_rows(min_row=2, min_col=4, max_col=5):
        for cell in row:
            cell.number_format = "0"
    for row in employee_sheet.iter_rows(min_row=2, min_col=6, max_col=11):
        for cell in row:
            cell.number_format = "¥#,##0.00"
    _style_sheet(employee_sheet, (12, 14, 12, 14, 12, 16, 16, 16, 14, 16, 16))

    department_sheet = workbook.create_sheet("部门汇总")
    department_sheet.append(
        [
            "部门编号",
            "部门名称",
            "成功员工数",
            "在职自然日",
            "基本工资",
            "无薪假扣款",
            "奖金",
            "年假结算",
            "部门应付薪资",
        ]
    )
    for summary in report.department_summaries:
        department_sheet.append(
            [
                summary.department_id,
                summary.department_name,
                summary.employee_count,
                summary.natural_days,
                _number(summary.base_pay),
                _number(summary.unpaid_leave_deduction),
                _number(summary.bonus),
                _number(summary.annual_leave_settlement),
                _number(summary.payable_salary),
            ]
        )
    department_sheet.append(
        ["合计", "", "", "", "", "", "", "", _number(report.total_payable_salary)]
    )
    for row in department_sheet.iter_rows(min_row=2, min_col=3, max_col=4):
        for cell in row:
            cell.number_format = "0"
    for row in department_sheet.iter_rows(min_row=2, min_col=5, max_col=9):
        for cell in row:
            cell.number_format = "¥#,##0.00"
    _style_sheet(department_sheet, (14, 18, 14, 14, 16, 16, 14, 16, 18))

    detail_sheet = workbook.create_sheet("核算明细")
    detail_sheet.append(
        [
            "月份",
            "员工编号",
            "姓名",
            "部门编号",
            "部门名称",
            "在职自然日",
            "月自然日",
            "月基本工资",
            "基本工资",
            "无薪假天数",
            "无薪假扣款",
            "奖金",
            "年假天数",
            "年假结算",
            "应付薪资",
            "离职日期",
        ]
    )
    for line in report.lines:
        detail_sheet.append(
            [
                report.month,
                line.employee_id,
                line.employee_name,
                line.department_id,
                line.department_name,
                line.employed_days,
                line.month_days,
                _number(line.monthly_base_salary),
                _number(line.base_pay),
                _number(line.unpaid_leave_days),
                _number(line.unpaid_leave_deduction),
                _number(line.bonus),
                _number(line.annual_leave_days),
                _number(line.annual_leave_settlement),
                _number(line.payable_salary),
                line.termination_date.isoformat() if line.termination_date else "",
            ]
        )
    for row in detail_sheet.iter_rows(min_row=2):
        for column_index in (6, 7):
            row[column_index - 1].number_format = "0"
        for column_index in (10, 13):
            row[column_index - 1].number_format = "0.00"
        for column_index in (8, 9, 11, 12, 14, 15):
            row[column_index - 1].number_format = "¥#,##0.00"
    _style_sheet(
        detail_sheet,
        (12, 14, 12, 14, 18, 14, 12, 16, 16, 14, 16, 14, 14, 16, 16, 14),
    )

    failure_sheet = workbook.create_sheet("失败明细")
    failure_sheet.append(["员工编号", "姓名", "失败原因"])
    for failure in report.failed_employees:
        failure_sheet.append([failure.employee_id, failure.employee_name, failure.reason])
    _style_sheet(failure_sheet, (14, 12, 64))

    rule_sheet = workbook.create_sheet("核算规则")
    rule_sheet.append(["项目", "规则"])
    rule_sheet.append(["核算范围", f"{report.month} / {report.department_id}"])
    rule_sheet.append(["部门基本工资", "部门在职自然日 ÷ 月自然日 × 月基本工资"])
    rule_sheet.append(["日薪", "月基本工资 ÷ 21.75"])
    rule_sheet.append(["无薪假", "休假天数 × 日薪，按休假日期归入部门"])
    rule_sheet.append(["奖金", "按奖金记录的部门编号归集"])
    rule_sheet.append(["年假结算", "年假余额 × 日薪，正数支付、负数冲减，归离职最终部门"])
    rule_sheet.append(["缺数据员工", "整人失败，不计入任何部门总额或全员总额"])
    rule_sheet.append(["金额精度", "保留两位小数，四舍五入"])
    _style_sheet(rule_sheet, (18, 72))

    file_name = f"payroll-{report.month}.xlsx"
    output_path = output_directory / file_name
    workbook.save(output_path)
    return output_path


def write_failure_summary(report: PayrollReport, output_directory: Path) -> Path:
    """生成可供模型通过会话文件工具读取的 UTF-8 失败摘要。

    参数:
        report: 已完成的薪资核算结果。
        output_directory: Athena 为本次 Skill 调用创建的输出目录。

    返回:
        已写入的失败摘要绝对路径。

    异常:
        输出目录或摘要无法写入时传播底层 ``OSError``。
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    successful_employee_count = len({line.employee_id for line in report.lines})
    summary_lines = [
        f"{report.month} {report.department_id} 月度薪资核算失败摘要",
        f"核算部门：{report.department_id}",
        f"成功员工：{successful_employee_count} 人",
        f"失败员工：{len(report.failed_employees)} 人",
        "",
        "失败员工详情：",
    ]
    if report.failed_employees:
        summary_lines.extend(
            f"- {failure.employee_id} {failure.employee_name}：{failure.reason}"
            for failure in report.failed_employees
        )
    else:
        summary_lines.append("- 无")
    summary_path = output_directory / f"payroll-{report.month}-failures.txt"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return summary_path


def _load_request() -> tuple[JsonObject, Path]:
    """读取 Athena 注入的 JSON 文件并返回参数与输出目录。

    参数:
        无参数；从 ``ATHENA_INPUT_PATH`` 读取输入位置。

    返回:
        Skill 参数对象和输出目录。

    异常:
        环境变量、JSON、参数结构或输出路径非法时抛出异常。
    """
    input_path = Path(os.environ["ATHENA_INPUT_PATH"])
    decoded = json.loads(input_path.read_text(encoding="utf-8"))
    request = _read_object(decoded, "request")
    parameters = _read_object(request.get("parameters"), "parameters")
    output_value = request.get("output_dir")
    if not isinstance(output_value, str) or not output_value:
        raise PayrollInputError("output_dir_invalid")
    return parameters, Path(output_value)


def main() -> None:
    """执行一次 Athena Skill 调用并写入 Excel 与输出清单。

    参数:
        无参数；使用 ``ATHENA_INPUT_PATH`` 指定的 JSON 请求。

    返回:
        无返回值；成功时向标准输出写入简短结果。

    异常:
        输入无法解析、核算失败或文件无法写入时以非零状态退出。
    """
    try:
        parameters, output_directory = _load_request()
        month = _required_text(parameters, "month", "parameters")
        department_id = _required_text(parameters, "department_id", "parameters")
        payroll_inputs = _read_object(
            parameters.get("payroll_inputs"),
            "parameters.payroll_inputs",
        )
        report = build_payroll_report(payroll_inputs, department_id)
        if report.month != month:
            raise PayrollInputError("parameters.month_mismatch")
        output_path = write_excel(report, output_directory)
        failure_summary_path = write_failure_summary(report, output_directory)
        manifest_path = output_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "outputs": [
                        {
                            "path": output_path.name,
                            "label": f"{report.month} {report.department_id} 月度薪资核算",
                        },
                        {
                            "path": failure_summary_path.name,
                            "label": f"{report.month} {report.department_id} 薪资核算失败摘要",
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
                    "successful_employees": len({line.employee_id for line in report.lines}),
                    "failed_employees": len(report.failed_employees),
                    "total_payable_salary": str(report.total_payable_salary),
                    "artifact": output_path.name,
                    "failure_summary": failure_summary_path.name,
                    "failed_employee_details": [
                        {
                            "employee_id": failure.employee_id,
                            "employee_name": failure.employee_name,
                            "reason": failure.reason,
                        }
                        for failure in report.failed_employees
                    ],
                },
                ensure_ascii=False,
            )
        )
    except (
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        PayrollInputError,
        ValueError,
    ) as error:
        raise SystemExit(f"payroll_skill_failed:{error}") from error


if __name__ == "__main__":
    main()
