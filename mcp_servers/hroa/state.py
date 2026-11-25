"""星辰智联 HR/OA 模拟 MCP Server 的并发安全 JSON 状态叠加层。

设计要点：
1. 每次读写均通过独立 ``.lock`` 文件加 ``fcntl.flock`` 排他/共享锁，确保 API 进程、
   Worker 进程以及 ``standard`` / ``hr_ops`` / ``fin_ops`` 三 Profile 并发访问时不损坏数据。
2. 写入一律采用“同目录临时文件 + flush/fsync + os.replace”原子替换，避免进程中断留下半截 JSON。
3. 所有变更仅记录在增量叠加文件内，执行 ``reset_state()`` 即可一键恢复 2026-11-24 基线。
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from mcp_servers.hroa.data import (
    BASELINE_DATE,
    BASELINE_DATE_BY_MONTH,
    COMPANY_NAME,
    DEPARTMENT_NAMES,
    MIN_LEAVE_STEP_DAYS,
    MONTHLY_PAYROLL_DAYS,
    PERFORMANCE_CALIBRATION_WINDOW,
    PERFORMANCE_CYCLE_DEFAULT,
    PERFORMANCE_DISTRIBUTION_GUIDE,
    SUPPORTED_MONTHS,
    SUPPORTED_YEAR,
    YEAR_END_CARRYOVER_RULE,
    Record,
    baseline_snapshot,
)

MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
LEGAL_REVIEW_CODE_PATTERN = re.compile(r"^LEG-REV-[0-9]{4}-[0-9]{4}$")
VALID_CLEARANCE_KEYS = ("work_handover", "it_asset", "finance_advance", "account_access")
VALID_CLEARANCE_STATUSES = frozenset({"cleared", "blocked", "pending"})
VALID_PERFORMANCE_RATINGS = frozenset({"S", "A", "B+", "B", "C"})
VALID_PERFORMANCE_ACTIONS = frozenset({"calibration_adjust", "appeal_review", "pip_confirm"})
VALID_IT_PRIORITIES = frozenset({"P0", "P1", "P2"})
IT_SLA_BY_PRIORITY: dict[str, dict[str, object]] = {
    "P0": {
        "response_sla": "15 分钟内响应",
        "resolve_sla": "4 小时内恢复",
        "sla_response_hours": 0.25,
        "sla_resolve_hours": 4,
    },
    "P1": {
        "response_sla": "1 小时内响应",
        "resolve_sla": "1 个工作日内解决",
        "sla_response_hours": 1,
        "sla_resolve_hours": 8,
    },
    "P2": {
        "response_sla": "4 小时内响应",
        "resolve_sla": "3 个工作日内解决",
        "sla_response_hours": 4,
        "sla_resolve_hours": 24,
    },
}
VALID_EXPENSE_AUDIT_DECISIONS = frozenset({"approved", "rejected_over_limit", "need_supplement"})
LEAVE_TYPE_ALIASES: dict[str, str] = {
    "annual": "annual",
    "年假": "annual",
    "comp_off": "comp_off",
    "调休": "comp_off",
    "调休假": "comp_off",
    "sick": "sick",
    "病假": "sick",
    "personal": "personal",
    "事假": "personal",
    "parental": "parental",
    "育儿假": "parental",
    "marriage": "marriage",
    "婚假": "marriage",
    "bereavement": "bereavement",
    "丧假": "bereavement",
}
CHANGE_TYPE_ALIASES: dict[str, str] = {
    "cross_dept_transfer": "cross_dept_transfer",
    "平调": "cross_dept_transfer",
    "跨部门平调": "cross_dept_transfer",
    "跨部门调岗": "cross_dept_transfer",
    "promotion": "promotion",
    "晋升": "promotion",
    "组内晋升": "promotion",
    "internal_transfer": "internal_transfer",
    "转岗": "internal_transfer",
    "内部转岗": "internal_transfer",
}


def _empty_overlay() -> dict[str, object]:
    """构造空的状态叠加结构（平滑兼容 V1 既有状态文件）。"""
    return {
        "schema_version": 1,
        "next_ticket_seq": 2001,
        "next_employee_seq": 1009,
        "added_employees": {},
        "employee_overrides": {},
        "leave_balance_overrides": {},
        "added_clearances": {},
        "clearance_item_overrides": {},
        "added_tickets": [],
        "ticket_status_overrides": {},
        "performance_overrides": {},
        "expense_audit_overrides": {},
    }


def _parse_iso_date(value: str, field_name: str) -> date:
    """严格校验 YYYY-MM-DD 格式日期。"""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name}_invalid: {field_name} 不能为空。")
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(
            f"{field_name}_invalid: 日期格式须为 YYYY-MM-DD（收到 {value!r}）。"
        ) from error


def _now_iso() -> str:
    """返回标准 UTC ISO 时间戳，供状态变更留痕。"""
    return datetime.now(tz=UTC).isoformat()


def _as_dict(value: object) -> dict[str, object]:
    """安全将叠加层中的对象节点收窄为字符串键字典。"""
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _as_list(value: object) -> list[dict[str, object]]:
    """安全将叠加层中的数组节点收窄为记录列表。"""
    if isinstance(value, list):
        return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]
    return []


def _as_float(value: object, default: float = 0.0) -> float:
    """安全读取数值字段为浮点数。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_int(value: object, default: int = 0) -> int:
    """安全读取整数字段。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _enrich_clearance(raw_clearance: dict[str, object]) -> dict[str, object]:
    """根据四项清查当前状态动态计算阻断项、待办项与离职证明签发资格。"""
    clearance = deepcopy(raw_clearance)
    items = _as_dict(clearance.get("items"))
    blocking_items: list[str] = []
    pending_items: list[str] = []
    cleared_items: list[str] = []
    for key in VALID_CLEARANCE_KEYS:
        item = _as_dict(items.get(key))
        status = str(item.get("status", "pending"))
        if status == "cleared":
            cleared_items.append(key)
        elif status == "blocked":
            blocking_items.append(key)
        else:
            pending_items.append(key)

    # 业务红线（XC-HR-2026-003）：仅当全部 4 项清查均为 cleared 时才允许开具离职证明。
    can_issue_certificate = len(cleared_items) == len(VALID_CLEARANCE_KEYS)
    clearance["blocking_items"] = blocking_items
    clearance["pending_items"] = pending_items
    clearance["cleared_items"] = cleared_items
    clearance["can_issue_certificate"] = can_issue_certificate
    return clearance


class HroaStateStore:
    """基于文件锁与原子替换的 HR/OA 模拟业务状态仓储。"""

    def __init__(self, state_path: Path) -> None:
        """保存状态文件路径及同级锁文件路径。"""
        self._state_path = state_path.expanduser().resolve()
        self._lock_path = self._state_path.with_name(f"{self._state_path.name}.lock")

    @property
    def state_path(self) -> Path:
        """返回状态文件绝对路径。"""
        return self._state_path

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        """对同级锁文件施加 flock 共享锁或排他锁，保护跨进程读写一致性。"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        with self._lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), lock_mode)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_overlay_unlocked(self) -> dict[str, object]:
        """在已持锁状态下读取叠加层 JSON；不存在时返回空叠加结构。"""
        if not self._state_path.is_file():
            return _empty_overlay()
        raw_text = self._state_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return _empty_overlay()
        parsed = json.loads(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("hroa_state_corrupted")
        overlay = _empty_overlay()
        overlay.update(cast(dict[str, object], parsed))
        return overlay

    def _write_overlay_unlocked(self, overlay: dict[str, object]) -> None:
        """在已持排他锁状态下以临时文件 + os.replace 原子写回 JSON。"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(overlay, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._state_path.parent,
                prefix=f".{self._state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(encoded)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, self._state_path)
            tmp_path = None
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def reset_state(self) -> dict[str, object]:
        """清空叠加状态并重置为 2026-11-24 基线快照。"""
        with self._locked(exclusive=True):
            self._write_overlay_unlocked(_empty_overlay())
        return {
            "status": "reset",
            "state_file": str(self._state_path),
            "baseline_date": BASELINE_DATE,
        }

    def _build_merged_state_unlocked(
        self,
        overlay: dict[str, object],
        *,
        month: str = "2026-11",
    ) -> tuple[
        list[Record],
        dict[str, Record],
        dict[str, Record],
        dict[str, Record],
        list[Record],
    ]:
        """将持久化叠加层合并到不可变月度基线快照上，产出当前一致视图。"""
        snapshot = baseline_snapshot()
        departments = _as_list(snapshot["departments"])
        employees = cast(dict[str, Record], snapshot["employees"])
        balances = cast(dict[str, Record], snapshot["leave_balances"])
        clearances = cast(dict[str, Record], snapshot["offboarding_clearances"])
        tickets = (
            _as_list(snapshot["december_oa_tickets"])
            if month == "2026-12"
            else _as_list(snapshot["oa_tickets"])
        )

        # 2026-12 月度视图先叠加 12 月演进快照，再叠加运行时持久化变更，确保不污染 2026-11 基线。
        if month == "2026-12":
            dec_patches = _as_dict(snapshot.get("december_employee_patches"))
            for emp_no, patch in dec_patches.items():
                if emp_no in employees and isinstance(patch, dict):
                    employees[emp_no].update(deepcopy(cast(Record, patch)))

        # 合并新发起入职的待报到员工与基线员工属性覆写。
        for emp_no, record in _as_dict(overlay.get("added_employees")).items():
            if isinstance(record, dict):
                employees[emp_no] = deepcopy(cast(Record, record))
        for emp_no, patch in _as_dict(overlay.get("employee_overrides")).items():
            if emp_no in employees and isinstance(patch, dict):
                employees[emp_no].update(deepcopy(cast(Record, patch)))

        # 合并假期余额覆写（如审批后扣减年假/调休或新增员工初始余额）。
        for emp_no, balance_patch in _as_dict(overlay.get("leave_balance_overrides")).items():
            if isinstance(balance_patch, dict):
                if emp_no in balances:
                    balances[emp_no].update(deepcopy(cast(Record, balance_patch)))
                else:
                    balances[emp_no] = deepcopy(cast(Record, balance_patch))

        # 合并新增离职清查单与四项清查项状态更新。
        for emp_no, clearance_rec in _as_dict(overlay.get("added_clearances")).items():
            if isinstance(clearance_rec, dict):
                clearances[emp_no] = deepcopy(cast(Record, clearance_rec))
        for emp_no, item_patches in _as_dict(overlay.get("clearance_item_overrides")).items():
            if emp_no not in clearances or not isinstance(item_patches, dict):
                continue
            target_items = _as_dict(clearances[emp_no].get("items"))
            for item_key, patch in item_patches.items():
                if item_key in target_items and isinstance(patch, dict):
                    _as_dict(target_items[item_key]).update(deepcopy(cast(Record, patch)))

        # 合并新建 OA 工单及已有工单状态同步。
        tickets.extend(deepcopy(_as_list(overlay.get("added_tickets"))))
        ticket_overrides = _as_dict(overlay.get("ticket_status_overrides"))
        for ticket in tickets:
            ticket_id = str(ticket.get("ticket_id", ""))
            if ticket_id in ticket_overrides and isinstance(ticket_overrides[ticket_id], dict):
                ticket.update(deepcopy(cast(Record, ticket_overrides[ticket_id])))

        enriched_clearances = {emp_no: _enrich_clearance(rec) for emp_no, rec in clearances.items()}
        return departments, employees, balances, enriched_clearances, tickets

    # ------------------------------------------------------------------
    # standard Profile V1 既有 6 个工具实现（V2 另增量追加 3 个工具，共 9 个）
    # ------------------------------------------------------------------

    def get_employee_profile(
        self,
        *,
        employee_no: str | None = None,
        name: str | None = None,
        department_id: str | None = None,
    ) -> dict[str, object]:
        """按工号、姓名或部门查询员工档案。"""
        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)

        matched: list[Record] = []
        norm_no = employee_no.strip().upper() if employee_no and employee_no.strip() else None
        norm_name = name.strip() if name and name.strip() else None
        norm_dept = department_id.strip() if department_id and department_id.strip() else None

        if norm_no is not None:
            record = employees.get(norm_no)
            if record is None:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            matched = [record]
        else:
            for record in employees.values():
                if norm_dept is not None and str(record.get("department_id")) != norm_dept:
                    continue
                if norm_name is not None and norm_name not in str(record.get("name", "")):
                    continue
                matched.append(record)

        return {
            "company_name": COMPANY_NAME,
            "baseline_date": BASELINE_DATE,
            "count": len(matched),
            "employee": matched[0] if len(matched) == 1 else None,
            "employees": matched,
        }

    def get_leave_balance(
        self, *, employee_no: str, year: int = SUPPORTED_YEAR
    ) -> dict[str, object]:
        """查询指定员工指定年度的年假、调休与病假使用余额。"""
        if year != SUPPORTED_YEAR:
            raise ValueError(
                f"leave_year_unsupported: 当前仅支持查询 {SUPPORTED_YEAR} 年度假期余额"
                f"（收到 {year}）。"
            )
        norm_no = employee_no.strip().upper()
        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            _, employees, balances, _, _ = self._build_merged_state_unlocked(overlay)

        employee = employees.get(norm_no)
        if employee is None:
            raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
        balance = balances.get(norm_no)
        if balance is None:
            raise ValueError(
                f"leave_balance_not_found: 未找到工号 {norm_no} 的 {year} 年余额记录。"
            )

        result = deepcopy(balance)
        result["employee_name"] = employee["name"]
        result["department_id"] = employee["department_id"]
        result["department_name"] = employee["department_name"]
        result["employment_status"] = employee["employment_status"]
        result["min_leave_step_days"] = MIN_LEAVE_STEP_DAYS
        result["monthly_payroll_days"] = MONTHLY_PAYROLL_DAYS
        result["baseline_date"] = BASELINE_DATE
        return result

    def list_oa_tickets(
        self,
        *,
        ticket_type: str | None = None,
        employee_no: str | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        """按工单类型、员工工号或状态筛选 OA 在办与历史工单。"""
        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            _, _, _, _, tickets = self._build_merged_state_unlocked(overlay)

        norm_type = ticket_type.strip().lower() if ticket_type and ticket_type.strip() else None
        norm_no = employee_no.strip().upper() if employee_no and employee_no.strip() else None
        norm_status = status.strip().lower() if status and status.strip() else None

        filtered: list[Record] = []
        for ticket in tickets:
            if norm_type is not None and str(ticket.get("ticket_type", "")).lower() != norm_type:
                continue
            if norm_no is not None and str(ticket.get("employee_no", "")).upper() != norm_no:
                continue
            if norm_status is not None and str(ticket.get("status", "")).lower() != norm_status:
                continue
            filtered.append(ticket)

        return {
            "baseline_date": BASELINE_DATE,
            "count": len(filtered),
            "tickets": filtered,
        }

    def check_offboarding_clearance(self, *, employee_no: str) -> dict[str, object]:
        """查询员工离职四项清查明细、阻断项与离职证明开具资格。"""
        norm_no = employee_no.strip().upper()
        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            _, employees, _, clearances, _ = self._build_merged_state_unlocked(overlay)

        if norm_no not in employees:
            raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
        clearance = clearances.get(norm_no)
        if clearance is None:
            raise ValueError(
                f"offboarding_clearance_not_found: 员工 {norm_no} 当前无在办或历史离职清查记录。"
            )
        clearance["baseline_date"] = BASELINE_DATE
        return clearance

    def submit_leave_request(
        self,
        *,
        employee_no: str,
        employee_name: str,
        leave_type: str,
        start_date: str,
        end_date: str,
        days: float,
        reason: str,
        handover_to: str,
    ) -> dict[str, object]:
        """校验员工状态、请假步长与余额后提交 OA 请假单，并原子扣减余额。"""
        norm_no = employee_no.strip().upper()
        norm_type = LEAVE_TYPE_ALIASES.get(leave_type.strip().lower(), "")
        if not norm_type:
            raise ValueError(
                f"leave_type_invalid: 不支持的假期类型 {leave_type!r}，"
                "仅支持 annual/comp_off/sick/personal/parental/marriage/bereavement。"
            )
        parsed_start = _parse_iso_date(start_date, "start_date")
        parsed_end = _parse_iso_date(end_date, "end_date")
        if parsed_end < parsed_start:
            raise ValueError("leave_date_range_invalid: 结束日期不得早于开始日期。")
        if (
            isinstance(days, bool)
            or not math.isfinite(days)
            or days <= 0
            or abs(days * 2 - round(days * 2)) > 1e-9
        ):
            raise ValueError(
                f"leave_days_invalid: 请假天数须为正数且为 0.5 天的整数倍（收到 {days}）。"
            )
        normalized_days = round(float(days), 2)
        max_span_days = (parsed_end - parsed_start).days + 1
        if normalized_days > max_span_days + 1e-9:
            raise ValueError(
                f"leave_days_exceed_date_range: 请假天数 {normalized_days:g} 天超过日期区间"
                f"（{parsed_start.isoformat()} 至 {parsed_end.isoformat()} "
                f"共 {max_span_days} 天）。"
            )
        if not reason.strip():
            raise ValueError("leave_reason_required: 请填写请假事由。")
        if not handover_to.strip():
            raise ValueError("handover_to_required: 请填写工作交接人。")

        # 排他锁保护：余额读取、充足性校验与余额扣减/落单在同一临界区内原子完成。
        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, balances, _, _ = self._build_merged_state_unlocked(overlay)

            employee = employees.get(norm_no)
            if employee is None:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            emp_status = str(employee.get("employment_status"))
            actual_name = str(employee.get("name"))
            if emp_status == "resigned":
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_no}（{actual_name}）已完成离职结算，"
                    "禁止提交请假申请。"
                )
            if emp_status == "preboarding":
                raise ValueError(
                    f"employee_not_onboarded: 员工 {norm_no}（{actual_name}）尚未入职报到，"
                    "禁止提交请假申请。"
                )
            if emp_status == "probation" and norm_type == "annual":
                # 依据 XC-HR-2026-001/002：试用期当年折算年假处于暂冻结状态，转正后解锁。
                raise ValueError(
                    f"probation_annual_leave_frozen: 员工 {norm_no}（{actual_name}）"
                    "当前处于试用期，当年折算年假暂冻结，转正考核通过后自动解锁。"
                )

            balance = deepcopy(balances[norm_no])
            annual_rem = _as_float(balance.get("annual_remaining_days"))
            comp_rem = _as_float(balance.get("comp_off_remaining_days"))

            if norm_type == "annual" and normalized_days > annual_rem + 1e-9:
                # 按 XC-HR-2026-001 拆假顺序给出精确可执行建议：剩余年假 -> 可用调休 -> 无薪事假。
                shortfall = round(normalized_days - annual_rem, 2)
                use_comp = round(min(comp_rem, shortfall), 2)
                use_personal = round(max(0.0, shortfall - use_comp), 2)
                raise ValueError(
                    f"leave_balance_insufficient: 员工 {norm_no}（{actual_name}）年假余额不足"
                    f"（申请 {normalized_days:g} 天，可用年假 {annual_rem:g} 天、"
                    f"调休 {comp_rem:g} 天，差额 {shortfall:g} 天）。按 XC-HR-2026-001 建议拆假："
                    f"年假 {annual_rem:g} 天 + 调休 {use_comp:g} 天 + 事假 {use_personal:g} 天。"
                )
            if norm_type == "comp_off" and normalized_days > comp_rem + 1e-9:
                raise ValueError(
                    f"leave_balance_insufficient: 员工 {norm_no}（{actual_name}）调休余额不足"
                    f"（申请 {normalized_days:g} 天，当前可用调休 {comp_rem:g} 天）。"
                )

            if norm_type == "annual":
                balance["annual_used_days"] = round(
                    _as_float(balance.get("annual_used_days")) + normalized_days, 2
                )
                balance["annual_remaining_days"] = round(annual_rem - normalized_days, 2)
                balance["statutory_annual_remaining_days"] = round(
                    min(
                        _as_float(balance.get("statutory_annual_remaining_days")),
                        _as_float(balance["annual_remaining_days"]),
                    ),
                    2,
                )
            elif norm_type == "comp_off":
                balance["comp_off_remaining_days"] = round(comp_rem - normalized_days, 2)
            elif norm_type == "sick":
                balance["sick_used_days"] = round(
                    _as_float(balance.get("sick_used_days")) + normalized_days, 2
                )

            seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = seq + 1
            ticket_id = f"OA-LVE-2026-{seq}"
            needs_medical_cert = norm_type == "sick" and normalized_days >= 2.0

            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "leave",
                "employee_no": norm_no,
                "employee_name": employee_name.strip() or actual_name,
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "status": "submitted",
                "submitted_at": _now_iso(),
                "summary": (
                    f"{actual_name}（{norm_no}）提交 {norm_type} {normalized_days:g} 天"
                    f"（{parsed_start.isoformat()} 至 {parsed_end.isoformat()}），"
                    f"交接人 {handover_to.strip()}"
                ),
                "details": {
                    "leave_type": norm_type,
                    "start_date": parsed_start.isoformat(),
                    "end_date": parsed_end.isoformat(),
                    "days": normalized_days,
                    "reason": reason.strip(),
                    "handover_to": handover_to.strip(),
                    "requires_medical_certificate": needs_medical_cert,
                },
            }

            balance_overrides = _as_dict(overlay.get("leave_balance_overrides"))
            balance_overrides[norm_no] = balance
            overlay["leave_balance_overrides"] = balance_overrides

            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        return {
            "ticket_id": ticket_id,
            "status": "submitted",
            "employee_no": norm_no,
            "employee_name": actual_name,
            "leave_type": norm_type,
            "start_date": parsed_start.isoformat(),
            "end_date": parsed_end.isoformat(),
            "days": normalized_days,
            "handover_to": handover_to.strip(),
            "requires_medical_certificate": needs_medical_cert,
            "remaining_balance": {
                "annual_remaining_days": balance["annual_remaining_days"],
                "comp_off_remaining_days": balance["comp_off_remaining_days"],
                "sick_used_days": balance["sick_used_days"],
            },
        }

    def submit_transfer_request(
        self,
        *,
        employee_no: str,
        employee_name: str,
        change_type: str,
        target_department_id: str,
        target_department_name: str,
        target_title: str,
        effective_date: str,
        reason: str,
    ) -> dict[str, object]:
        """校验在职状态与目标部门后提交员工调岗或晋升异动 OA 工单。"""
        norm_no = employee_no.strip().upper()
        norm_change = CHANGE_TYPE_ALIASES.get(change_type.strip().lower(), "")
        if not norm_change:
            raise ValueError(
                f"change_type_invalid: 不支持的异动类型 {change_type!r}，"
                "仅支持 cross_dept_transfer/promotion/internal_transfer。"
            )
        norm_target_dept = target_department_id.strip()
        if norm_target_dept not in DEPARTMENT_NAMES:
            raise ValueError(f"department_not_found: 目标部门 {norm_target_dept!r} 不存在。")
        parsed_effective = _parse_iso_date(effective_date, "effective_date")
        if not target_title.strip():
            raise ValueError("target_title_required: 目标岗位不能为空。")
        if not reason.strip():
            raise ValueError("transfer_reason_required: 异动原因不能为空。")

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, balances, _, _ = self._build_merged_state_unlocked(overlay)

            employee = employees.get(norm_no)
            if employee is None:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            emp_status = str(employee.get("employment_status"))
            actual_name = str(employee.get("name"))
            if emp_status == "resigned":
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_no}（{actual_name}）"
                    "已离职停用，禁止提交异动申请。"
                )
            if emp_status == "offboarding":
                raise ValueError(
                    f"employee_in_offboarding: 员工 {norm_no}（{actual_name}）"
                    "正在办理离职，禁止提交异动申请。"
                )

            annual_rem = _as_float(balances.get(norm_no, {}).get("annual_remaining_days"))
            seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = seq + 1
            ticket_id = f"OA-TRF-2026-{seq}"
            resolved_dept_name = (
                target_department_name.strip() or DEPARTMENT_NAMES[norm_target_dept]
            )

            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "promotion" if norm_change == "promotion" else "transfer",
                "employee_no": norm_no,
                "employee_name": employee_name.strip() or actual_name,
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "status": "pending_approval",
                "submitted_at": _now_iso(),
                "summary": (
                    f"{actual_name}（{norm_no}）发起 {norm_change}："
                    f"{employee['department_name']} → {resolved_dept_name}"
                    f"（{target_title.strip()}），生效日 {parsed_effective.isoformat()}"
                ),
                "details": {
                    "change_type": norm_change,
                    "source_department_id": employee["department_id"],
                    "target_department_id": norm_target_dept,
                    "target_department_name": resolved_dept_name,
                    "target_title": target_title.strip(),
                    "effective_date": parsed_effective.isoformat(),
                    "reason": reason.strip(),
                    "annual_leave_carryover": True,
                    "annual_leave_carryover_days": annual_rem,
                },
            }

            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        return {
            "ticket_id": ticket_id,
            "status": "pending_approval",
            "employee_no": norm_no,
            "employee_name": actual_name,
            "change_type": norm_change,
            "source_department_id": employee["department_id"],
            "target_department_id": norm_target_dept,
            "target_department_name": resolved_dept_name,
            "target_title": target_title.strip(),
            "effective_date": parsed_effective.isoformat(),
            "annual_leave_carryover": True,
            "annual_leave_carryover_days": annual_rem,
        }

    # ------------------------------------------------------------------
    # hr_ops Profile V1 既有 4 个工具实现（V2 另增量追加 1 个工具，共 5 个）
    # ------------------------------------------------------------------

    def list_department_roster(
        self,
        *,
        department_id: str = "ALL",
        month: str = "2026-11",
    ) -> dict[str, object]:
        """导出供 SK-5 消费的 schema_version=1 月度（2026-11/2026-12）结构化花名册数据。"""
        norm_month = month.strip()
        if MONTH_PATTERN.fullmatch(norm_month) is None:
            raise ValueError(f"month_invalid: 月份格式须为 YYYY-MM（收到 {month!r}）。")
        if norm_month not in SUPPORTED_MONTHS:
            raise ValueError(
                f"month_unsupported: 当前仅支持查询 {', '.join(SUPPORTED_MONTHS)} "
                f"月份花名册数据（收到 {norm_month!r}）。"
            )
        norm_dept = department_id.strip() or "ALL"
        if norm_dept != "ALL" and norm_dept not in DEPARTMENT_NAMES:
            raise ValueError(f"department_not_found: 部门编号 {norm_dept!r} 不存在。")

        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            departments, employees, balances, clearances, tickets = (
                self._build_merged_state_unlocked(overlay, month=norm_month)
            )

        if norm_dept == "ALL":
            scoped_employees = list(employees.values())
            scoped_emp_nos = set(employees)
        else:
            scoped_employees = [
                emp for emp in employees.values() if str(emp.get("department_id")) == norm_dept
            ]
            scoped_emp_nos = {str(emp["employee_no"]) for emp in scoped_employees}

        scoped_balances = [bal for emp_no, bal in balances.items() if emp_no in scoped_emp_nos]
        scoped_tickets = [
            ticket for ticket in tickets if str(ticket.get("employee_no", "")) in scoped_emp_nos
        ]
        scoped_clearances = [
            clearance for emp_no, clearance in clearances.items() if emp_no in scoped_emp_nos
        ]

        payload: dict[str, object] = {
            "schema_version": 1,
            "company_name": COMPANY_NAME,
            "baseline_date": BASELINE_DATE_BY_MONTH[norm_month],
            "month": norm_month,
            "department_id": norm_dept,
            "departments": departments,
            "employees": scoped_employees,
            "leave_balances": scoped_balances,
            "oa_tickets": scoped_tickets,
            "offboarding_clearances": scoped_clearances,
        }
        # 2026-12 月度视图补充跨年年假结转规则（2026-11 保持与 V1/V2 结构 100% 一致）。
        if norm_month == "2026-12":
            payload["year_end_carryover_rule"] = deepcopy(YEAR_END_CARRYOVER_RULE)
        return payload

    def initiate_onboarding(
        self,
        *,
        offer_code: str,
        employee_name: str,
        department_id: str,
        department_name: str,
        job_title: str,
        start_date: str,
        mentor_no: str,
        laptop_model: str,
    ) -> dict[str, object]:
        """HR 专属：依据 Offer 为待报到新员工建立入职档案与 OA 入职办理工单。"""
        norm_offer = offer_code.strip().upper()
        norm_name = employee_name.strip()
        norm_dept = department_id.strip()
        norm_mentor = mentor_no.strip().upper()
        if not norm_offer:
            raise ValueError("offer_code_required: Offer 编号不能为空。")
        if not norm_name:
            raise ValueError("employee_name_required: 新员工姓名不能为空。")
        if norm_dept not in DEPARTMENT_NAMES:
            raise ValueError(f"department_not_found: 入职部门 {norm_dept!r} 不存在。")
        if not job_title.strip():
            raise ValueError("job_title_required: 入职岗位不能为空。")
        parsed_start = _parse_iso_date(start_date, "start_date")
        if not laptop_model.strip():
            raise ValueError("laptop_model_required: 配发设备型号不能为空。")

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)

            mentor = employees.get(norm_mentor)
            if mentor is None or str(mentor.get("employment_status")) == "resigned":
                raise ValueError(f"mentor_invalid: 入职导师 {norm_mentor!r} 不存在或已离职。")
            for existing in employees.values():
                if str(existing.get("offer_code") or "").upper() == norm_offer:
                    raise ValueError(
                        f"onboarding_offer_already_exists: Offer {norm_offer} 已发起入职"
                        f"（工号 {existing['employee_no']}）。"
                    )

            emp_seq = _as_int(overlay.get("next_employee_seq"), 1009)
            overlay["next_employee_seq"] = emp_seq + 1
            new_emp_no = f"XC{emp_seq}"

            ticket_seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = ticket_seq + 1
            ticket_id = f"OA-ONB-2026-{ticket_seq}"

            resolved_dept_name = department_name.strip() or DEPARTMENT_NAMES[norm_dept]
            new_employee: Record = {
                "employee_no": new_emp_no,
                "mock_user_id": None,
                "name": norm_name,
                "department_id": norm_dept,
                "department_name": resolved_dept_name,
                "job_title": job_title.strip(),
                "level": "P6",
                "employment_status": "preboarding",
                "hire_date": parsed_start.isoformat(),
                "work_city": "上海张江",
                "city_tier": "tier1",
                "manager_no": str(mentor.get("manager_no") or "MOCK001"),
                "email": f"{new_emp_no.lower()}@xingchen.test",
                "probation_end_date": None,
                "last_working_day": None,
                "offer_code": norm_offer,
                "mentor_no": norm_mentor,
                "laptop_model": laptop_model.strip(),
                "notes": f"Offer {norm_offer} 已发起入职筹备，报到日 {parsed_start.isoformat()}",
            }
            new_balance: Record = {
                "employee_no": new_emp_no,
                "year": SUPPORTED_YEAR,
                "statutory_annual_total": 0.0,
                "tenure_annual_total": 0.0,
                "annual_total_days": 0.0,
                "annual_used_days": 0.0,
                "annual_remaining_days": 0.0,
                "statutory_annual_remaining_days": 0.0,
                "comp_off_remaining_days": 0.0,
                "sick_used_days": 0.0,
                "paid_sick_quota_days": 5.0,
            }
            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "onboarding",
                "employee_no": new_emp_no,
                "employee_name": norm_name,
                "department_id": norm_dept,
                "department_name": resolved_dept_name,
                "status": "pending_checkin",
                "submitted_at": _now_iso(),
                "summary": (
                    f"Offer {norm_offer} 为 {norm_name}（{new_emp_no}）发起入职，"
                    f"报到日 {parsed_start.isoformat()}，导师 {norm_mentor}，"
                    f"配机 {laptop_model.strip()}"
                ),
                "details": {
                    "offer_code": norm_offer,
                    "start_date": parsed_start.isoformat(),
                    "mentor_no": norm_mentor,
                    "laptop_model": laptop_model.strip(),
                },
            }

            added_employees = _as_dict(overlay.get("added_employees"))
            added_employees[new_emp_no] = new_employee
            overlay["added_employees"] = added_employees

            balance_overrides = _as_dict(overlay.get("leave_balance_overrides"))
            balance_overrides[new_emp_no] = new_balance
            overlay["leave_balance_overrides"] = balance_overrides

            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        return {
            "ticket_id": ticket_id,
            "employee_no": new_emp_no,
            "employee_name": norm_name,
            "offer_code": norm_offer,
            "department_id": norm_dept,
            "department_name": resolved_dept_name,
            "job_title": job_title.strip(),
            "start_date": parsed_start.isoformat(),
            "mentor_no": norm_mentor,
            "laptop_model": laptop_model.strip(),
            "status": "pending_checkin",
        }

    def initiate_offboarding(
        self,
        *,
        employee_no: str,
        employee_name: str,
        last_working_day: str,
        leave_reason_type: str,
        handover_to: str,
        settlement_note: str = "",
    ) -> dict[str, object]:
        """HR 专属：为在职/试用期员工发起离职流程并初始化四项清查记录。"""
        norm_no = employee_no.strip().upper()
        parsed_lwd = _parse_iso_date(last_working_day, "last_working_day")
        if not leave_reason_type.strip():
            raise ValueError("leave_reason_type_required: 离职原因类型不能为空。")
        if not handover_to.strip():
            raise ValueError("handover_to_required: 工作交接人不能为空。")

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, balances, _, _ = self._build_merged_state_unlocked(overlay)

            employee = employees.get(norm_no)
            if employee is None:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            emp_status = str(employee.get("employment_status"))
            actual_name = str(employee.get("name"))
            if emp_status == "resigned":
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_no}（{actual_name}）已完成离职结算，"
                    "禁止重复发起离职流程。"
                )
            if emp_status == "offboarding":
                raise ValueError(
                    f"employee_already_offboarding: 员工 {norm_no}（{actual_name}）已在离职办理中。"
                )

            now_ts = _now_iso()
            unused_statutory = _as_float(
                balances.get(norm_no, {}).get("statutory_annual_remaining_days")
            )
            clearance: Record = {
                "employee_no": norm_no,
                "employee_name": employee_name.strip() or actual_name,
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "last_working_day": parsed_lwd.isoformat(),
                "leave_reason_type": leave_reason_type.strip(),
                "handover_to": handover_to.strip(),
                "unused_statutory_annual_days": unused_statutory,
                "certificate_no": None,
                "items": {
                    "work_handover": {
                        "label": "工作与文档交接",
                        "status": "pending",
                        "owner_dept": str(employee["department_name"]),
                        "detail": f"待向 {handover_to.strip()} 完成工作与文档交接",
                        "evidence_note": settlement_note.strip() or "离职流程初始化",
                        "updated_at": now_ts,
                    },
                    "it_asset": {
                        "label": "IT 资产设备归还",
                        "status": "pending",
                        "owner_dept": "行政与 IT 部",
                        "detail": (
                            f"待归还配发设备（{employee.get('laptop_model', '办公设备')}）与门禁卡"
                        ),
                        "evidence_note": "待 IT 验收",
                        "updated_at": now_ts,
                    },
                    "finance_advance": {
                        "label": "财务备用金与报销核销",
                        "status": "pending",
                        "owner_dept": "财务部",
                        "detail": "待财务核验在途差旅报销与备用金核销状态",
                        "evidence_note": "待财务签批",
                        "updated_at": now_ts,
                    },
                    "account_access": {
                        "label": "域账号与系统权限回收",
                        "status": "pending",
                        "owner_dept": "行政与 IT 部",
                        "detail": (
                            f"待最后工作日 {parsed_lwd.isoformat()} 18:00 统一回收账号与系统权限"
                        ),
                        "evidence_note": "待最后工作日执行回收",
                        "updated_at": now_ts,
                    },
                },
            }

            ticket_seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = ticket_seq + 1
            ticket_id = f"OA-OFF-2026-{ticket_seq}"
            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "offboarding",
                "employee_no": norm_no,
                "employee_name": actual_name,
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "status": "in_progress",
                "submitted_at": now_ts,
                "summary": (
                    f"已为 {actual_name}（{norm_no}）发起离职流程（{leave_reason_type.strip()}），"
                    f"最后工作日 {parsed_lwd.isoformat()}，交接人 {handover_to.strip()}"
                ),
                "details": {
                    "last_working_day": parsed_lwd.isoformat(),
                    "leave_reason_type": leave_reason_type.strip(),
                    "handover_to": handover_to.strip(),
                    "unused_statutory_annual_days": unused_statutory,
                    "settlement_note": settlement_note.strip(),
                },
            }

            employee_overrides = _as_dict(overlay.get("employee_overrides"))
            employee_overrides[norm_no] = {
                "employment_status": "offboarding",
                "last_working_day": parsed_lwd.isoformat(),
            }
            overlay["employee_overrides"] = employee_overrides

            added_clearances = _as_dict(overlay.get("added_clearances"))
            added_clearances[norm_no] = clearance
            overlay["added_clearances"] = added_clearances

            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        enriched = _enrich_clearance(clearance)
        return {
            "ticket_id": ticket_id,
            "employee_no": norm_no,
            "employee_name": actual_name,
            "employment_status": "offboarding",
            "last_working_day": parsed_lwd.isoformat(),
            "unused_statutory_annual_days": unused_statutory,
            "can_issue_certificate": enriched["can_issue_certificate"],
            "clearance": enriched,
        }

    def update_clearance_item(
        self,
        *,
        employee_no: str,
        employee_name: str,
        item_key: str,
        new_status: str,
        evidence_note: str,
    ) -> dict[str, object]:
        """HR 专属：更新指定离职员工的单个清查项并重算离职证明开具资格。"""
        norm_no = employee_no.strip().upper()
        norm_key = item_key.strip()
        norm_status = new_status.strip().lower()
        if norm_key not in VALID_CLEARANCE_KEYS:
            raise ValueError(
                f"clearance_item_invalid: 清查项 {item_key!r} 非法，"
                f"仅支持 {', '.join(VALID_CLEARANCE_KEYS)}。"
            )
        if norm_status not in VALID_CLEARANCE_STATUSES:
            raise ValueError(
                f"clearance_status_invalid: 清查状态 {new_status!r} 非法，"
                "仅支持 cleared/blocked/pending。"
            )
        if not evidence_note.strip():
            raise ValueError("evidence_note_required: 请填写核销凭证与操作备注。")

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, _, clearances, tickets = self._build_merged_state_unlocked(overlay)

            if norm_no not in employees:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            employee = employees[norm_no]
            if str(employee.get("employment_status")) == "resigned":
                # 依据 XC-HR-2026-003：已离职封存档案严禁篡改历史清查记录。
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_no}（{employee.get('name')}）"
                    "已完成离职清算并封存档案，禁止修改历史离职清查记录。"
                )
            if norm_no not in clearances:
                raise ValueError(
                    f"offboarding_clearance_not_found: 员工 {norm_no} 当前无在办离职清查记录。"
                )

            now_ts = _now_iso()
            all_overrides = _as_dict(overlay.get("clearance_item_overrides"))
            emp_overrides = _as_dict(all_overrides.get(norm_no))
            emp_overrides[norm_key] = {
                "status": norm_status,
                "evidence_note": evidence_note.strip(),
                "updated_at": now_ts,
            }
            all_overrides[norm_no] = emp_overrides
            overlay["clearance_item_overrides"] = all_overrides

            # 先按更新后的叠加层重算四项清查结果，同步刷新离职 OA 工单状态与 details 明细。
            _, _, _, refreshed_clearances, _ = self._build_merged_state_unlocked(overlay)
            updated_clearance = refreshed_clearances[norm_no]
            can_issue = bool(updated_clearance["can_issue_certificate"])
            blocking = list(cast(list[str], updated_clearance["blocking_items"]))
            pending = list(cast(list[str], updated_clearance["pending_items"]))
            ticket_status = (
                "ready_for_certificate" if can_issue else ("blocked" if blocking else "in_progress")
            )

            ticket_overrides = _as_dict(overlay.get("ticket_status_overrides"))
            for ticket in tickets:
                if (
                    str(ticket.get("ticket_type")) == "offboarding"
                    and str(ticket.get("employee_no")).upper() == norm_no
                    and str(ticket.get("status")) != "completed"
                ):
                    ticket_id = str(ticket["ticket_id"])
                    # 基于原工单 details 浅拷贝同步阻断项与出证资格，避免列表与清查视图漂移。
                    updated_details = dict(_as_dict(ticket.get("details")))
                    updated_details["blocking_items"] = blocking
                    updated_details["pending_items"] = pending
                    updated_details["can_issue_certificate"] = can_issue
                    ticket_overrides[ticket_id] = {
                        "status": ticket_status,
                        "details": updated_details,
                    }
            overlay["ticket_status_overrides"] = ticket_overrides
            self._write_overlay_unlocked(overlay)

        return {
            "employee_no": norm_no,
            "employee_name": employee_name.strip() or str(updated_clearance["employee_name"]),
            "updated_item_key": norm_key,
            "new_status": norm_status,
            "evidence_note": evidence_note.strip(),
            "can_issue_certificate": updated_clearance["can_issue_certificate"],
            "blocking_items": updated_clearance["blocking_items"],
            "pending_items": updated_clearance["pending_items"],
            "cleared_items": updated_clearance["cleared_items"],
            "clearance": updated_clearance,
        }

    # ------------------------------------------------------------------
    # V2 扩展辅助方法：合并绩效基线与差旅报销稽核状态
    # ------------------------------------------------------------------

    def _build_v2_views_unlocked(
        self, overlay: dict[str, object]
    ) -> tuple[dict[str, Record], list[Record], Record]:
        """合并 2026 年度绩效基线及 2026-11 / 2026-12 共 12 笔差旅报销单的持久化叠加变更。"""
        snapshot = baseline_snapshot()
        reviews = cast(dict[str, Record], snapshot["performance_reviews"])
        expenses = _as_list(snapshot["travel_expenses"])
        travel_standards = cast(Record, snapshot["travel_standards"])

        # 合并已持久化的绩效校准/申诉/PIP 覆写记录。
        for emp_no, patch in _as_dict(overlay.get("performance_overrides")).items():
            if not isinstance(patch, dict):
                continue
            if emp_no in reviews:
                reviews[emp_no].update(deepcopy(cast(Record, patch)))
            else:
                reviews[emp_no] = deepcopy(cast(Record, patch))

        # 合并财务人员持久化的差旅报销稽核结论。
        expense_overrides = _as_dict(overlay.get("expense_audit_overrides"))
        for expense in expenses:
            expense_id = str(expense.get("expense_id", ""))
            patch = expense_overrides.get(expense_id)
            if isinstance(patch, dict):
                expense.update(deepcopy(cast(Record, patch)))

        return reviews, expenses, travel_standards

    # ------------------------------------------------------------------
    # V2 standard Profile 新增 3 个工具：绩效查询、合同用印、IT 工单
    # ------------------------------------------------------------------

    def get_performance_review(
        self,
        *,
        employee_no: str,
        cycle: str = PERFORMANCE_CYCLE_DEFAULT,
    ) -> dict[str, object]:
        """查询员工指定考核周期的绩效初评/校准等级、PIP 状态与申诉截止日。"""
        norm_cycle = cycle.strip() or PERFORMANCE_CYCLE_DEFAULT
        if norm_cycle != PERFORMANCE_CYCLE_DEFAULT:
            raise ValueError(
                f"performance_cycle_unsupported: 当前仅支持查询 {PERFORMANCE_CYCLE_DEFAULT} 周期"
                f"（收到 {cycle!r}）。"
            )
        norm_no = employee_no.strip().upper()
        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)
            reviews, _, _ = self._build_v2_views_unlocked(overlay)

        employee = employees.get(norm_no)
        if employee is None:
            raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")

        review = reviews.get(norm_no)
        if review is None:
            # 对运行期新发起入职的待报到员工，返回不参与本周期定档的结构化结果。
            emp_status = str(employee.get("employment_status", "preboarding"))
            review = {
                "employee_no": norm_no,
                "employee_name": employee["name"],
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "cycle": norm_cycle,
                "rating": None,
                "status": f"not_applicable_{emp_status}",
                "eligible": False,
                "pip_required": False,
                "pip_days": 0,
                "appeal_deadline": None,
                "reviewer_no": employee.get("manager_no"),
                "calibration_history": [],
                "notes": "新入职待报到员工，不参与当前年度绩效定档",
            }
        else:
            review = deepcopy(review)

        review["company_name"] = COMPANY_NAME
        review["baseline_date"] = BASELINE_DATE
        review["employment_status"] = employee["employment_status"]
        review["calibration_window"] = {
            "start_date": PERFORMANCE_CALIBRATION_WINDOW[0],
            "end_date": PERFORMANCE_CALIBRATION_WINDOW[1],
        }
        review["distribution_guide"] = deepcopy(PERFORMANCE_DISTRIBUTION_GUIDE)
        return review

    def submit_contract_seal_request(
        self,
        *,
        applicant_no: str,
        applicant_name: str,
        contract_no: str,
        contract_title: str,
        counterparty: str,
        amount_cny: float,
        seal_type: str,
        legal_review_code: str,
        is_blank_contract: bool = False,
        special_clause_flag: bool = False,
    ) -> dict[str, object]:
        """按 XC-LEG-2026-008 校验红线与三档审批链，提交并持久化合同用印 OA 工单。"""
        # 红线守卫 1：严禁任何形式的空白合同用印。
        if is_blank_contract:
            raise ValueError(
                "blank_contract_forbidden: 依据 XC-LEG-2026-008 严禁空白合同用印，"
                "请补齐全部合同条款后再申请。"
            )
        # 红线守卫 2：必须持有有效法务初审编号（LEG-REV-YYYY-NNNN）。
        norm_legal_code = legal_review_code.strip().upper()
        if not norm_legal_code or LEGAL_REVIEW_CODE_PATTERN.fullmatch(norm_legal_code) is None:
            raise ValueError(
                "legal_review_code_required: 依据 XC-LEG-2026-008 严禁缺失有效法务初审编号用印，"
                f"格式须为 LEG-REV-YYYY-NNNN（收到 {legal_review_code!r}）。"
            )
        # 红线守卫 3：合同金额必须为正有限数值。
        if isinstance(amount_cny, bool) or not math.isfinite(amount_cny) or amount_cny <= 0:
            raise ValueError(
                "contract_amount_invalid: 合同金额须为大于 0 的有效人民币金额"
                f"（收到 {amount_cny}）。"
            )
        norm_applicant = applicant_no.strip().upper()
        norm_contract_no = contract_no.strip().upper()
        norm_title = contract_title.strip()
        norm_counterparty = counterparty.strip()
        norm_seal_type = seal_type.strip()
        if not norm_contract_no:
            raise ValueError("contract_no_required: 合同编号不能为空。")
        if not norm_title:
            raise ValueError("contract_title_required: 合同名称不能为空。")
        if not norm_counterparty:
            raise ValueError("counterparty_required: 签约相对方不能为空。")
        if not norm_seal_type:
            raise ValueError("seal_type_required: 用印类型不能为空。")

        normalized_amount = round(float(amount_cny), 2)

        # 按 XC-LEG-2026-008 自动判定三档审批链（含排他/无限连带责任特批升级规则）。
        if normalized_amount > 500000.0 or special_clause_flag:
            approval_tier = "tier_3_group_head_final"
            approval_chain = [
                "部门负责人",
                "法务初审",
                "财务负责人（林清岚 MOCK002）",
                "集团负责人（周明远 MOCK001）",
            ]
            ticket_status = "pending_group_head_approval"
        elif normalized_amount > 100000.0:
            approval_tier = "tier_2_finance_countersign"
            approval_chain = [
                "部门负责人",
                "法务初审",
                "财务负责人（林清岚 MOCK002）",
            ]
            ticket_status = "pending_finance_countersign"
        else:
            approval_tier = "tier_1_dept_and_legal"
            approval_chain = ["部门负责人", "法务初审"]
            ticket_status = "pending_dept_and_legal_approval"

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)

            applicant = employees.get(norm_applicant)
            if applicant is None:
                raise ValueError(
                    f"employee_not_found: 未找到工号为 {norm_applicant} 的申请人档案。"
                )
            actual_name = str(applicant.get("name"))
            if str(applicant.get("employment_status")) == "resigned":
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_applicant}（{actual_name}）已离职停用，"
                    "禁止发起合同用印申请。"
                )

            seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = seq + 1
            ticket_id = f"OA-SEAL-2026-{seq}"

            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "contract_seal",
                "employee_no": norm_applicant,
                "employee_name": applicant_name.strip() or actual_name,
                "department_id": applicant["department_id"],
                "department_name": applicant["department_name"],
                "status": ticket_status,
                "submitted_at": _now_iso(),
                "summary": (
                    f"{actual_name}（{norm_applicant}）提交合同 {norm_contract_no}《{norm_title}》"
                    f"用印申请（{norm_seal_type}，金额 {normalized_amount:,.2f} 元，"
                    f"法务初审 {norm_legal_code}）"
                ),
                "details": {
                    "contract_no": norm_contract_no,
                    "contract_title": norm_title,
                    "counterparty": norm_counterparty,
                    "amount_cny": normalized_amount,
                    "seal_type": norm_seal_type,
                    "legal_review_code": norm_legal_code,
                    "is_blank_contract": False,
                    "special_clause_flag": bool(special_clause_flag),
                    "approval_tier": approval_tier,
                    "approval_chain": approval_chain,
                },
            }

            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        return {
            "ticket_id": ticket_id,
            "status": ticket_status,
            "applicant_no": norm_applicant,
            "applicant_name": actual_name,
            "contract_no": norm_contract_no,
            "contract_title": norm_title,
            "counterparty": norm_counterparty,
            "amount_cny": normalized_amount,
            "seal_type": norm_seal_type,
            "legal_review_code": norm_legal_code,
            "special_clause_flag": bool(special_clause_flag),
            "approval_tier": approval_tier,
            "approval_chain": approval_chain,
        }

    def submit_it_asset_request(
        self,
        *,
        employee_no: str,
        employee_name: str,
        request_type: str,
        item_spec: str,
        priority: str,
        justification: str,
    ) -> dict[str, object]:
        """按 XC-IT-2026-009 校验优先级与在职状态，提交并持久化 IT 资产或权限工单。"""
        norm_no = employee_no.strip().upper()
        norm_type = request_type.strip()
        norm_spec = item_spec.strip()
        norm_priority = priority.strip().upper()
        norm_justification = justification.strip()

        if norm_priority not in VALID_IT_PRIORITIES:
            raise ValueError(
                f"it_priority_invalid: 不支持的 IT 工单优先级 {priority!r}，仅允许 P0/P1/P2。"
            )
        if not norm_type:
            raise ValueError("it_request_type_required: IT 申请类型不能为空。")
        if not norm_spec:
            raise ValueError("it_item_spec_required: 申请设备或权限规格不能为空。")
        if not norm_justification:
            raise ValueError("it_justification_required: 申请事由不能为空。")

        sla_meta = deepcopy(IT_SLA_BY_PRIORITY[norm_priority])

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)

            employee = employees.get(norm_no)
            if employee is None:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            actual_name = str(employee.get("name"))
            if str(employee.get("employment_status")) == "resigned":
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_no}（{actual_name}）已离职停用，"
                    "禁止提交 IT 资产或权限工单。"
                )

            seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = seq + 1
            ticket_id = f"OA-IT-2026-{seq}"

            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "it_asset",
                "employee_no": norm_no,
                "employee_name": employee_name.strip() or actual_name,
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "status": "submitted",
                "submitted_at": _now_iso(),
                "summary": (
                    f"{norm_priority} IT 工单（{norm_type}）：{actual_name}（{norm_no}）"
                    f"申请 {norm_spec}"
                    f"（SLA：{sla_meta['response_sla']} / {sla_meta['resolve_sla']}）"
                ),
                "details": {
                    "request_type": norm_type,
                    "item_spec": norm_spec,
                    "priority": norm_priority,
                    "justification": norm_justification,
                    **sla_meta,
                },
            }

            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        return {
            "ticket_id": ticket_id,
            "status": "submitted",
            "employee_no": norm_no,
            "employee_name": actual_name,
            "request_type": norm_type,
            "item_spec": norm_spec,
            "priority": norm_priority,
            "justification": norm_justification,
            "sla": sla_meta,
        }

    # ------------------------------------------------------------------
    # V2 hr_ops Profile 新增 1 个工具：绩效校准与申诉/PIP 审批落盘
    # ------------------------------------------------------------------

    def submit_performance_calibration(
        self,
        *,
        employee_no: str,
        employee_name: str,
        proposed_rating: str,
        action_type: str,
        rationale: str,
        operator_no: str,
    ) -> dict[str, object]:
        """HR 专属：执行绩效校准调档、申诉复议或 PIP 确认，并实时同步绩效档案。"""
        norm_no = employee_no.strip().upper()
        norm_rating = proposed_rating.strip().upper()
        norm_action = action_type.strip().lower()
        norm_rationale = rationale.strip()
        norm_operator = operator_no.strip().upper()

        if norm_rating not in VALID_PERFORMANCE_RATINGS:
            raise ValueError(
                f"performance_rating_invalid: 不支持的绩效等级 {proposed_rating!r}，"
                "仅允许 S/A/B+/B/C。"
            )
        if norm_action not in VALID_PERFORMANCE_ACTIONS:
            raise ValueError(
                f"performance_action_invalid: 不支持的校准动作 {action_type!r}，"
                "仅允许 calibration_adjust/appeal_review/pip_confirm。"
            )
        if norm_action == "pip_confirm" and norm_rating != "C":
            # 依据 XC-HR-2026-007：仅 C 档触发 60 天 PIP，严禁对非 C 档执行 pip_confirm。
            raise ValueError(
                "pip_confirm_requires_rating_c: 仅绩效等级为 C 时允许执行 pip_confirm "
                f"确认 60 天 PIP 改善计划（收到 {norm_rating}）。"
            )
        if not norm_rationale:
            raise ValueError("calibration_rationale_required: 校准或复议依据说明不能为空。")
        if not norm_operator:
            raise ValueError("operator_no_required: 操作人工号不能为空。")

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)
            reviews, _, _ = self._build_v2_views_unlocked(overlay)

            employee = employees.get(norm_no)
            if employee is None:
                raise ValueError(f"employee_not_found: 未找到工号为 {norm_no} 的员工档案。")
            actual_name = str(employee.get("name"))
            emp_status = str(employee.get("employment_status"))
            if emp_status == "resigned":
                raise ValueError(
                    f"employee_already_resigned: 员工 {norm_no}（{actual_name}）"
                    "已离职封存绩效档案，禁止执行校准或修改。"
                )
            if emp_status in {"preboarding", "probation"}:
                raise ValueError(
                    f"performance_cycle_not_eligible: 员工 {norm_no}（{actual_name}）"
                    f"当前处于 {emp_status} 状态，不参与 2026 年度绩效定档与校准。"
                )
            existing_review = reviews.get(norm_no, {})
            if emp_status == "offboarding" or str(existing_review.get("status")) == (
                "offboarding_frozen"
            ):
                # 离职办理中员工绩效处于冻结归档态（如 XC1003），严禁修改年度绩效等级。
                raise ValueError(
                    f"performance_offboarding_frozen: 员工 {norm_no}（{actual_name}）"
                    "处于离职冻结期，禁止修改年度绩效等级。"
                )

            operator = employees.get(norm_operator)
            if operator is None or str(operator.get("employment_status")) == "resigned":
                raise ValueError(f"operator_invalid: 操作人 {norm_operator!r} 不存在或已离职。")

            current_review = deepcopy(reviews[norm_no])
            previous_rating = current_review.get("rating")
            new_status = {
                "calibration_adjust": "calibrated",
                "appeal_review": "appeal_resolved",
                "pip_confirm": "pip_active",
            }[norm_action]
            pip_required = norm_rating == "C"
            pip_days = 60 if pip_required else 0
            now_ts = _now_iso()

            history = _as_list(current_review.get("calibration_history"))
            history.append(
                {
                    "action_type": norm_action,
                    "previous_rating": previous_rating,
                    "proposed_rating": norm_rating,
                    "rationale": norm_rationale,
                    "operator_no": norm_operator,
                    "operator_name": str(operator.get("name")),
                    "recorded_at": now_ts,
                }
            )
            current_review["rating"] = norm_rating
            current_review["status"] = new_status
            current_review["pip_required"] = pip_required
            current_review["pip_days"] = pip_days
            current_review["calibration_history"] = history
            current_review["notes"] = norm_rationale

            perf_overrides = _as_dict(overlay.get("performance_overrides"))
            perf_overrides[norm_no] = current_review
            overlay["performance_overrides"] = perf_overrides

            seq = _as_int(overlay.get("next_ticket_seq"), 2001)
            overlay["next_ticket_seq"] = seq + 1
            ticket_id = f"OA-PERF-2026-{seq}"
            ticket: Record = {
                "ticket_id": ticket_id,
                "ticket_type": "performance_calibration",
                "employee_no": norm_no,
                "employee_name": employee_name.strip() or actual_name,
                "department_id": employee["department_id"],
                "department_name": employee["department_name"],
                "status": new_status,
                "submitted_at": now_ts,
                "summary": (
                    f"绩效 {norm_action}：{actual_name}（{norm_no}）"
                    f" {previous_rating or '-'} → {norm_rating}（操作人 {norm_operator}）"
                ),
                "details": {
                    "cycle": PERFORMANCE_CYCLE_DEFAULT,
                    "action_type": norm_action,
                    "previous_rating": previous_rating,
                    "proposed_rating": norm_rating,
                    "pip_required": pip_required,
                    "pip_days": pip_days,
                    "rationale": norm_rationale,
                    "operator_no": norm_operator,
                },
            }
            added_tickets = _as_list(overlay.get("added_tickets"))
            added_tickets.append(ticket)
            overlay["added_tickets"] = added_tickets
            self._write_overlay_unlocked(overlay)

        return {
            "ticket_id": ticket_id,
            "employee_no": norm_no,
            "employee_name": actual_name,
            "cycle": PERFORMANCE_CYCLE_DEFAULT,
            "action_type": norm_action,
            "previous_rating": previous_rating,
            "current_rating": norm_rating,
            "status": new_status,
            "pip_required": pip_required,
            "pip_days": pip_days,
            "operator_no": norm_operator,
            "rationale": norm_rationale,
        }

    # ------------------------------------------------------------------
    # V2 fin_ops Profile 2 个工具：差旅报销清单导出与财务稽核结论标注
    # ------------------------------------------------------------------

    def list_travel_expenses(
        self,
        *,
        month: str = "2026-11",
        department_id: str = "ALL",
    ) -> dict[str, object]:
        """财务专属：导出 schema_version=1 月度（2026-11/2026-12）差旅报销单与备用金关联数据。"""
        norm_month = month.strip()
        if MONTH_PATTERN.fullmatch(norm_month) is None:
            raise ValueError(f"month_invalid: 月份格式须为 YYYY-MM（收到 {month!r}）。")
        if norm_month not in SUPPORTED_MONTHS:
            raise ValueError(
                f"month_unsupported: 当前仅支持查询 {', '.join(SUPPORTED_MONTHS)} "
                f"月份差旅报销数据（收到 {norm_month!r}）。"
            )
        norm_dept = department_id.strip() or "ALL"
        if norm_dept != "ALL" and norm_dept not in DEPARTMENT_NAMES:
            raise ValueError(f"department_not_found: 部门编号 {norm_dept!r} 不存在。")

        with self._locked(exclusive=False):
            overlay = self._read_overlay_unlocked()
            departments, _, _, clearances, _ = self._build_merged_state_unlocked(
                overlay, month=norm_month
            )
            _, expenses, travel_standards = self._build_v2_views_unlocked(overlay)

        scoped_expenses = [
            exp
            for exp in expenses
            if str(exp.get("month")) == norm_month
            and (norm_dept == "ALL" or str(exp.get("department_id")) == norm_dept)
        ]

        # 按部门汇总报销笔数、申报总额与待核销备用金金额，便于财务与 SK-7 交叉校验。
        dept_bucket: dict[str, dict[str, object]] = {}
        for exp in scoped_expenses:
            dept_id = str(exp["department_id"])
            dept_name = str(exp["department_name"])
            entry = dept_bucket.setdefault(
                dept_id,
                {
                    "department_id": dept_id,
                    "department_name": dept_name,
                    "expense_count": 0,
                    "total_claimed_cny": 0.0,
                    "unreconciled_advance_cny": 0.0,
                },
            )
            entry["expense_count"] = _as_int(entry["expense_count"]) + 1
            entry["total_claimed_cny"] = round(
                _as_float(entry["total_claimed_cny"]) + _as_float(exp.get("total_claimed_cny")),
                2,
            )
            if bool(exp.get("advance_unreconciled")):
                entry["unreconciled_advance_cny"] = round(
                    _as_float(entry["unreconciled_advance_cny"])
                    + _as_float(exp.get("advance_amount_cny")),
                    2,
                )

        scoped_emp_nos = {str(exp.get("employee_no")) for exp in scoped_expenses}
        related_clearances = [
            clearance for emp_no, clearance in clearances.items() if emp_no in scoped_emp_nos
        ]

        return {
            "schema_version": 1,
            "company_name": COMPANY_NAME,
            "baseline_date": BASELINE_DATE_BY_MONTH[norm_month],
            "month": norm_month,
            "department_id": norm_dept,
            "departments": departments,
            "travel_standards": travel_standards,
            "expenses": scoped_expenses,
            "department_summary": [dept_bucket[key] for key in sorted(dept_bucket)],
            "related_clearances": related_clearances,
        }

    def flag_expense_audit_result(
        self,
        *,
        expense_id: str,
        decision: str,
        audit_note: str,
        auditor_no: str,
    ) -> dict[str, object]:
        """财务专属：对指定差旅报销单持久化稽核放行、超标驳回或退回补件结论。"""
        norm_expense_id = expense_id.strip().upper()
        norm_decision = decision.strip().lower()
        norm_note = audit_note.strip()
        norm_auditor = auditor_no.strip().upper()

        if norm_decision not in VALID_EXPENSE_AUDIT_DECISIONS:
            raise ValueError(
                f"expense_audit_decision_invalid: 不支持的稽核结论 {decision!r}，"
                "仅允许 approved/rejected_over_limit/need_supplement。"
            )
        if not norm_note:
            raise ValueError("audit_note_required: 稽核意见与制度依据说明不能为空。")
        if not norm_auditor:
            raise ValueError("auditor_no_required: 稽核人员工号不能为空。")

        with self._locked(exclusive=True):
            overlay = self._read_overlay_unlocked()
            _, employees, _, _, _ = self._build_merged_state_unlocked(overlay)
            _, expenses, _ = self._build_v2_views_unlocked(overlay)

            auditor = employees.get(norm_auditor)
            if auditor is None or str(auditor.get("employment_status")) == "resigned":
                raise ValueError(f"auditor_invalid: 稽核人 {norm_auditor!r} 不存在或已离职。")

            target_expense = next(
                (
                    exp
                    for exp in expenses
                    if str(exp.get("expense_id", "")).upper() == norm_expense_id
                ),
                None,
            )
            if target_expense is None:
                raise ValueError(
                    f"travel_expense_not_found: 未找到单号为 {norm_expense_id} 的差旅报销单。"
                )

            now_ts = _now_iso()
            overrides = _as_dict(overlay.get("expense_audit_overrides"))
            overrides[norm_expense_id] = {
                "audit_status": norm_decision,
                "audit_note": norm_note,
                "audited_by": norm_auditor,
                "audited_at": now_ts,
            }
            overlay["expense_audit_overrides"] = overrides
            self._write_overlay_unlocked(overlay)

        return {
            "expense_id": norm_expense_id,
            "employee_no": target_expense["employee_no"],
            "employee_name": target_expense["employee_name"],
            "department_id": target_expense["department_id"],
            "department_name": target_expense["department_name"],
            "decision": norm_decision,
            "audit_status": norm_decision,
            "audit_note": norm_note,
            "auditor_no": norm_auditor,
            "auditor_name": str(auditor.get("name")),
            "audited_at": now_ts,
        }


__all__ = [
    "CHANGE_TYPE_ALIASES",
    "HroaStateStore",
    "IT_SLA_BY_PRIORITY",
    "LEAVE_TYPE_ALIASES",
    "LEGAL_REVIEW_CODE_PATTERN",
    "VALID_CLEARANCE_KEYS",
    "VALID_CLEARANCE_STATUSES",
    "VALID_EXPENSE_AUDIT_DECISIONS",
    "VALID_IT_PRIORITIES",
    "VALID_PERFORMANCE_ACTIONS",
    "VALID_PERFORMANCE_RATINGS",
]
