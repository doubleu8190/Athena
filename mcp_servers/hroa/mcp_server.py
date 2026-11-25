"""星辰智联统一 HR/OA 模拟 stdio MCP Server（V1+V2+V3 三 Profile 16 工具，2026-11/2026-12）。

支持三 Profile 启动以验证 Athena 三层工具权限隔离与多月份演进快照：
- ``--profile standard``：暴露 9 个全员可用 HR/OA 查询与自助提交工具；
- ``--profile hr_ops``：暴露 5 个 HR 专属花名册导出（2026-11/2026-12）、绩效校准与高危办单工具；
- ``--profile fin_ops``：暴露 2 个财务专属差旅报销导出（2026-11/2026-12）与稽核结论标注工具。

所有诊断日志严格写入 ``sys.stderr``，确保 ``stdout`` 仅承载标准 MCP JSON-RPC 协议帧。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# 兼容容器绝对脚本路径启动（/usr/local/bin/python /app/mcp_servers/hroa/mcp_server.py）
# 与包导入（import mcp_servers.hroa.mcp_server）。
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp_servers.hroa.data import PERFORMANCE_CYCLE_DEFAULT, SUPPORTED_YEAR  # noqa: E402
from mcp_servers.hroa.state import HroaStateStore  # noqa: E402

STANDARD_PROFILE = "standard"
HR_OPS_PROFILE = "hr_ops"
FIN_OPS_PROFILE = "fin_ops"
VALID_PROFILES = (STANDARD_PROFILE, HR_OPS_PROFILE, FIN_OPS_PROFILE)

logger = logging.getLogger("athena.mcp.hroa")


def _configure_stderr_logging() -> None:
    """将诊断日志严格绑定到 stderr，绝不污染 stdio MCP 协议输出流。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [hroa-mcp] %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def resolve_state_path(cli_state_file: str | None) -> Path:
    """按 CLI 参数 > 环境变量 HROA_STATE_PATH > 临时兜底目录顺序解析状态文件路径。"""
    if cli_state_file and cli_state_file.strip():
        return Path(cli_state_file.strip()).expanduser().resolve()
    env_value = os.environ.get("HROA_STATE_PATH", "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return (Path(tempfile.gettempdir()) / "athena-hroa-test" / "state.json").resolve()


def _call_store(action: Callable[[], dict[str, object]]) -> dict[str, object]:
    """执行仓储业务动作，将可预期业务 ValueError 转译为 MCP ToolError 以透传错误码。"""
    try:
        return action()
    except ValueError as error:
        # MCP SDK 2.3 默认屏蔽普通异常详情，显式抛出 ToolError 才能让模型与调用方读到业务错误码。
        raise ToolError(str(error)) from error


def create_hroa_server(*, profile: str, state_path: Path) -> MCPServer:
    """根据指定 Profile 构造并注册精确工具集合的 MCPServer 实例。"""
    if profile not in VALID_PROFILES:
        raise ValueError(f"hroa_profile_invalid: 仅支持 {VALID_PROFILES}（收到 {profile!r}）。")

    store = HroaStateStore(state_path)
    server_name_map = {
        STANDARD_PROFILE: "athena-hroa-standard",
        HR_OPS_PROFILE: "athena-hroa-hr-ops",
        FIN_OPS_PROFILE: "athena-hroa-fin-ops",
    }
    server = MCPServer(server_name_map[profile])

    if profile == STANDARD_PROFILE:

        @server.tool()
        async def hroa_get_employee_profile(
            employee_no: str | None = None,
            name: str | None = None,
            department_id: str | None = None,
        ) -> dict[str, object]:
            """查询星辰智联员工档案（支持按工号、姓名模糊匹配或部门编号筛选）。"""
            return _call_store(
                lambda: store.get_employee_profile(
                    employee_no=employee_no,
                    name=name,
                    department_id=department_id,
                )
            )

        @server.tool()
        async def hroa_get_leave_balance(
            employee_no: str,
            year: int = SUPPORTED_YEAR,
        ) -> dict[str, object]:
            """查询指定员工年度年假（法定+司龄福利）、调休及病假已用余额。"""
            return _call_store(lambda: store.get_leave_balance(employee_no=employee_no, year=year))

        @server.tool()
        async def hroa_list_oa_tickets(
            ticket_type: str | None = None,
            employee_no: str | None = None,
            status: str | None = None,
        ) -> dict[str, object]:
            """查询星辰智联 OA 在办及归档工单（入转调离、用印、IT 与绩效）。"""
            return _call_store(
                lambda: store.list_oa_tickets(
                    ticket_type=ticket_type,
                    employee_no=employee_no,
                    status=status,
                )
            )

        @server.tool()
        async def hroa_check_offboarding_clearance(employee_no: str) -> dict[str, object]:
            """核查指定员工离职四项清查状态、阻断明细及是否具备开具离职证明条件。"""
            return _call_store(lambda: store.check_offboarding_clearance(employee_no=employee_no))

        @server.tool()
        async def hroa_submit_leave_request(
            employee_no: str,
            employee_name: str,
            leave_type: str,
            start_date: str,
            end_date: str,
            days: float,
            reason: str,
            handover_to: str,
        ) -> dict[str, object]:
            """提交员工请假申请并扣减对应假期余额（需人工审批，拒绝离职员工及超额申请）。"""
            return _call_store(
                lambda: store.submit_leave_request(
                    employee_no=employee_no,
                    employee_name=employee_name,
                    leave_type=leave_type,
                    start_date=start_date,
                    end_date=end_date,
                    days=days,
                    reason=reason,
                    handover_to=handover_to,
                )
            )

        @server.tool()
        async def hroa_submit_transfer_request(
            employee_no: str,
            employee_name: str,
            change_type: str,
            target_department_id: str,
            target_department_name: str,
            target_title: str,
            effective_date: str,
            reason: str,
        ) -> dict[str, object]:
            """提交员工跨部门平调、内部转岗或组内晋升异动申请（需人工审批）。"""
            return _call_store(
                lambda: store.submit_transfer_request(
                    employee_no=employee_no,
                    employee_name=employee_name,
                    change_type=change_type,
                    target_department_id=target_department_id,
                    target_department_name=target_department_name,
                    target_title=target_title,
                    effective_date=effective_date,
                    reason=reason,
                )
            )

        @server.tool()
        async def hroa_get_performance_review(
            employee_no: str,
            cycle: str = PERFORMANCE_CYCLE_DEFAULT,
        ) -> dict[str, object]:
            """查询员工 2026 年度绩效初评/校准等级、PIP 改善状态及申诉窗口截止日。"""
            return _call_store(
                lambda: store.get_performance_review(employee_no=employee_no, cycle=cycle)
            )

        @server.tool()
        async def hroa_submit_contract_seal_request(
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
            """提交合同用印申请（需人工审批；自动校验法务初审编号、严禁空白用印并匹配三档审批链）。"""
            return _call_store(
                lambda: store.submit_contract_seal_request(
                    applicant_no=applicant_no,
                    applicant_name=applicant_name,
                    contract_no=contract_no,
                    contract_title=contract_title,
                    counterparty=counterparty,
                    amount_cny=amount_cny,
                    seal_type=seal_type,
                    legal_review_code=legal_review_code,
                    is_blank_contract=is_blank_contract,
                    special_clause_flag=special_clause_flag,
                )
            )

        @server.tool()
        async def hroa_submit_it_asset_request(
            employee_no: str,
            employee_name: str,
            request_type: str,
            item_spec: str,
            priority: str,
            justification: str,
        ) -> dict[str, object]:
            """提交 IT 资产设备或系统权限申请工单（需人工审批；按 P0/P1/P2 自动标注 SLA 时限）。"""
            return _call_store(
                lambda: store.submit_it_asset_request(
                    employee_no=employee_no,
                    employee_name=employee_name,
                    request_type=request_type,
                    item_spec=item_spec,
                    priority=priority,
                    justification=justification,
                )
            )

        return server

    if profile == HR_OPS_PROFILE:
        # hr_ops Profile：仅向人力资源部授权的 5 个 HR 内部运维、绩效校准与高危办单工具。
        @server.tool()
        async def hroa_list_department_roster(
            department_id: str = "ALL",
            month: str = "2026-11",
        ) -> dict[str, object]:
            """导出 schema_version=1 部门月度花名册、余额、工单与清查数据（2026-11/2026-12）。"""
            return _call_store(
                lambda: store.list_department_roster(department_id=department_id, month=month)
            )

        @server.tool()
        async def hroa_initiate_onboarding(
            offer_code: str,
            employee_name: str,
            department_id: str,
            department_name: str,
            job_title: str,
            start_date: str,
            mentor_no: str,
            laptop_model: str,
        ) -> dict[str, object]:
            """HR 专属：依据 Offer 为待报到新员工发起入职建档与配机导师筹备流程（需人工审批）。"""
            return _call_store(
                lambda: store.initiate_onboarding(
                    offer_code=offer_code,
                    employee_name=employee_name,
                    department_id=department_id,
                    department_name=department_name,
                    job_title=job_title,
                    start_date=start_date,
                    mentor_no=mentor_no,
                    laptop_model=laptop_model,
                )
            )

        @server.tool()
        async def hroa_initiate_offboarding(
            employee_no: str,
            employee_name: str,
            last_working_day: str,
            leave_reason_type: str,
            handover_to: str,
            settlement_note: str = "",
        ) -> dict[str, object]:
            """HR 专属：为在职员工发起离职流程并初始化四项清查阻断追踪单（需人工审批）。"""
            return _call_store(
                lambda: store.initiate_offboarding(
                    employee_no=employee_no,
                    employee_name=employee_name,
                    last_working_day=last_working_day,
                    leave_reason_type=leave_reason_type,
                    handover_to=handover_to,
                    settlement_note=settlement_note,
                )
            )

        @server.tool()
        async def hroa_update_clearance_item(
            employee_no: str,
            employee_name: str,
            item_key: str,
            new_status: str,
            evidence_note: str,
        ) -> dict[str, object]:
            """HR 专属：更新离职四项清查项状态并重算离职证明开具资格。"""
            return _call_store(
                lambda: store.update_clearance_item(
                    employee_no=employee_no,
                    employee_name=employee_name,
                    item_key=item_key,
                    new_status=new_status,
                    evidence_note=evidence_note,
                )
            )

        @server.tool()
        async def hroa_submit_performance_calibration(
            employee_no: str,
            employee_name: str,
            proposed_rating: str,
            action_type: str,
            rationale: str,
            operator_no: str,
        ) -> dict[str, object]:
            """HR 专属：提交绩效校准调档、申诉复议或 PIP 改善确认（需人工审批）。"""
            return _call_store(
                lambda: store.submit_performance_calibration(
                    employee_no=employee_no,
                    employee_name=employee_name,
                    proposed_rating=proposed_rating,
                    action_type=action_type,
                    rationale=rationale,
                    operator_no=operator_no,
                )
            )

        return server

    # fin_ops Profile：仅向财务部授权的 2 个差旅报销稽核与结论落盘工具。
    @server.tool()
    async def hroa_list_travel_expenses(
        month: str = "2026-11",
        department_id: str = "ALL",
    ) -> dict[str, object]:
        """财务专属：导出 schema_version=1 月度差旅报销明细与备用金数据（2026-11/2026-12）。"""
        return _call_store(
            lambda: store.list_travel_expenses(month=month, department_id=department_id)
        )

    @server.tool()
    async def hroa_flag_expense_audit_result(
        expense_id: str,
        decision: str,
        audit_note: str,
        auditor_no: str,
    ) -> dict[str, object]:
        """财务专属：对指定差旅报销单落盘稽核结论（approved/rejected_over_limit/need_supplement）。"""
        return _call_store(
            lambda: store.flag_expense_audit_result(
                expense_id=expense_id,
                decision=decision,
                audit_note=audit_note,
                auditor_no=auditor_no,
            )
        )

    return server


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数，支持三 Profile、自定义状态文件与状态重置。"""
    parser = argparse.ArgumentParser(
        description="星辰智联科技有限公司统一 HR/OA 模拟 stdio MCP Server",
    )
    parser.add_argument(
        "--profile",
        choices=VALID_PROFILES,
        default=STANDARD_PROFILE,
        help=(
            "启动工具集 Profile：standard（全员 9 工具）、"
            "hr_ops（HR 专属 5 工具）或 fin_ops（财务专属 2 工具）"
        ),
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="共享状态叠加 JSON 路径（默认读取 HROA_STATE_PATH 或系统临时目录）",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="重置共享状态文件为 2026-11-24 基线数据后立即退出 0",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """CLI 主入口：支持复位状态或按 Profile 启动 stdio MCP Server。"""
    _configure_stderr_logging()
    args = parse_args(argv)
    state_path = resolve_state_path(args.state_file)
    if args.reset_state:
        result = HroaStateStore(state_path).reset_state()
        logger.info("state_reset path=%s baseline_date=%s", state_path, result["baseline_date"])
        return

    logger.info("starting_server profile=%s state_file=%s", args.profile, state_path)
    server = create_hroa_server(profile=args.profile, state_path=state_path)
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
