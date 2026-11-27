"""星辰智联统一 HR/OA 三 Profile（16 工具）stdio MCP Server 端到端集成测试。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from mcp import Client

from infrastructure.mcp.stdio import McpStdioTransport

REPO_ROOT = Path(__file__).resolve().parents[3]
HROA_DIR = REPO_ROOT / "mcp_servers" / "hroa"
HROA_SERVER_PATH = HROA_DIR / "mcp_server.py"

EXPECTED_STANDARD_TOOLS = [
    "hroa_get_employee_profile",
    "hroa_get_leave_balance",
    "hroa_list_oa_tickets",
    "hroa_check_offboarding_clearance",
    "hroa_submit_leave_request",
    "hroa_submit_transfer_request",
    "hroa_get_performance_review",
    "hroa_submit_contract_seal_request",
    "hroa_submit_it_asset_request",
]
EXPECTED_HR_OPS_TOOLS = [
    "hroa_list_department_roster",
    "hroa_initiate_onboarding",
    "hroa_initiate_offboarding",
    "hroa_update_clearance_item",
    "hroa_submit_performance_calibration",
]
EXPECTED_FIN_OPS_TOOLS = [
    "hroa_list_travel_expenses",
    "hroa_flag_expense_audit_result",
]


@asynccontextmanager
async def _hroa_client(profile: str, state_file: Path) -> AsyncIterator[Client]:
    """启动真实 stdio 子进程并连接指定 Profile 的 HR/OA MCP Server。"""
    transport = McpStdioTransport(
        sys.executable,
        (
            str(HROA_SERVER_PATH),
            "--profile",
            profile,
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
        yield client


def _error_text(result: Any) -> str:
    """提取 MCP 工具错误返回中的可读文本内容。"""
    parts: list[str] = []
    for block in getattr(result, "content", ()):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def test_hroa_three_profiles_expose_exact_sixteen_disjoint_tools(tmp_path: Path) -> None:
    """standard（9）、hr_ops（5）与 fin_ops（2）三 Profile 精确暴露 16 个无重叠工具。"""
    state_file = tmp_path / "hroa-state.json"

    async def scenario() -> None:
        async with _hroa_client("standard", state_file) as std_client:
            std_tools = [tool.name for tool in (await std_client.list_tools()).tools]
            assert std_tools == EXPECTED_STANDARD_TOOLS

        async with _hroa_client("hr_ops", state_file) as hr_client:
            hr_tools = [tool.name for tool in (await hr_client.list_tools()).tools]
            assert hr_tools == EXPECTED_HR_OPS_TOOLS

        async with _hroa_client("fin_ops", state_file) as fin_client:
            fin_tools = [tool.name for tool in (await fin_client.list_tools()).tools]
            assert fin_tools == EXPECTED_FIN_OPS_TOOLS

        all_sets = (
            set(EXPECTED_STANDARD_TOOLS),
            set(EXPECTED_HR_OPS_TOOLS),
            set(EXPECTED_FIN_OPS_TOOLS),
        )
        assert len(set().union(*all_sets)) == 16
        assert not (all_sets[0] & all_sets[1])
        assert not (all_sets[0] & all_sets[2])
        assert not (all_sets[1] & all_sets[2])

    asyncio.run(scenario())


def test_hroa_all_ten_tools_state_persistence_clearance_progression_and_reset(
    tmp_path: Path,
) -> None:
    """端到端覆盖全部 10 个工具、跨进程状态持久化、XC1003 清查三步流转与 --reset-state 复位。"""
    state_file = tmp_path / "shared-state.json"

    async def scenario() -> None:
        # 1. standard Profile 只读基线核验。
        async with _hroa_client("standard", state_file) as std:
            prof_res = await std.call_tool("hroa_get_employee_profile", {"employee_no": "XC1001"})
            assert prof_res.is_error is False
            prof_data = cast(dict[str, Any], prof_res.structured_content)
            assert prof_data["count"] == 1
            assert prof_data["employee"]["name"] == "陈思远"
            assert prof_data["employee"]["offer_code"] == "OFFER-2026-1108"

            bal_res = await std.call_tool("hroa_get_leave_balance", {"employee_no": "MOCK005"})
            assert bal_res.is_error is False
            bal_data = cast(dict[str, Any], bal_res.structured_content)
            assert bal_data["annual_remaining_days"] == 4.5
            assert bal_data["comp_off_remaining_days"] == 1.5

            # 提交合法请假（MOCK005 请 1.5 天年假）与合法调岗（XC1004 跨部门平调）。
            leave_res = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "MOCK005",
                    "employee_name": "陆知微",
                    "leave_type": "annual",
                    "start_date": "2026-12-07",
                    "end_date": "2026-12-08",
                    "days": 1.5,
                    "reason": "家庭事务休假",
                    "handover_to": "MOCK003",
                },
            )
            assert leave_res.is_error is False
            leave_data = cast(dict[str, Any], leave_res.structured_content)
            assert leave_data["status"] == "submitted"
            assert leave_data["remaining_balance"]["annual_remaining_days"] == 3.0
            new_leave_ticket_id = str(leave_data["ticket_id"])

            trf_res = await std.call_tool(
                "hroa_submit_transfer_request",
                {
                    "employee_no": "XC1004",
                    "employee_name": "郑语桐",
                    "change_type": "cross_dept_transfer",
                    "target_department_id": "dept_mock_hr",
                    "target_department_name": "人力资源部",
                    "target_title": "薪酬绩效专员",
                    "effective_date": "2026-12-01",
                    "reason": "内部跨部门发展平调",
                },
            )
            assert trf_res.is_error is False
            trf_data = cast(dict[str, Any], trf_res.structured_content)
            assert trf_data["annual_leave_carryover"] is True
            assert trf_data["annual_leave_carryover_days"] == 4.0

            # 核查 XC1003 初始双阻断状态。
            clr0_res = await std.call_tool(
                "hroa_check_offboarding_clearance", {"employee_no": "XC1003"}
            )
            assert clr0_res.is_error is False
            clr0 = cast(dict[str, Any], clr0_res.structured_content)
            assert clr0["can_issue_certificate"] is False
            assert set(clr0["blocking_items"]) == {"it_asset", "finance_advance"}
            assert clr0["pending_items"] == ["account_access"]

        # 2. 切换至新子进程（hr_ops Profile），验证共享状态可读且可执行 4 个 HR 专属工具。
        async with _hroa_client("hr_ops", state_file) as hr:
            roster_res = await hr.call_tool(
                "hroa_list_department_roster",
                {"department_id": "ALL", "month": "2026-11"},
            )
            assert roster_res.is_error is False
            roster = cast(dict[str, Any], roster_res.structured_content)
            assert roster["schema_version"] == 1
            assert len(roster["employees"]) == 14
            ticket_ids = {str(t["ticket_id"]) for t in roster["oa_tickets"]}
            assert new_leave_ticket_id in ticket_ids

            # 发起新员工入职。
            onb_res = await hr.call_tool(
                "hroa_initiate_onboarding",
                {
                    "offer_code": "OFFER-2026-1199",
                    "employee_name": "周子航",
                    "department_id": "dept_mock_platform",
                    "department_name": "平台架构组",
                    "job_title": "高级分布式工程师",
                    "start_date": "2026-12-10",
                    "mentor_no": "MOCK005",
                    "laptop_model": "MacBook Pro 14 (M4 Pro / 36G / 1TB)",
                },
            )
            assert onb_res.is_error is False
            onb_data = cast(dict[str, Any], onb_res.structured_content)
            assert onb_data["employee_no"] == "XC1009"

            # 为在职员工 XC1007 发起离职流程。
            off_res = await hr.call_tool(
                "hroa_initiate_offboarding",
                {
                    "employee_no": "XC1007",
                    "employee_name": "唐逸凡",
                    "last_working_day": "2026-12-25",
                    "leave_reason_type": "个人主动离职",
                    "handover_to": "MOCK005",
                    "settlement_note": "测试发起离职流程",
                },
            )
            assert off_res.is_error is False
            off_data = cast(dict[str, Any], off_res.structured_content)
            assert off_data["can_issue_certificate"] is False

            # XC1003 步骤一：仅核销财务备用金 -> 仍受 IT 阻断，不可提前出证。
            u1_res = await hr.call_tool(
                "hroa_update_clearance_item",
                {
                    "employee_no": "XC1003",
                    "employee_name": "韩景明",
                    "item_key": "finance_advance",
                    "new_status": "cleared",
                    "evidence_note": "已收到银行回单退回 2,400.00 元（FIN-ADV-2026-0914）",
                },
            )
            assert u1_res.is_error is False
            u1 = cast(dict[str, Any], u1_res.structured_content)
            assert u1["blocking_items"] == ["it_asset"]
            assert u1["can_issue_certificate"] is False

            # 验证在办离职 OA 工单的 details.blocking_items 与清查更新同步，不残留过期字段。
            mid_roster = cast(
                dict[str, Any],
                (
                    await hr.call_tool(
                        "hroa_list_department_roster",
                        {"department_id": "dept_mock_platform", "month": "2026-11"},
                    )
                ).structured_content,
            )
            mid_off_ticket = next(
                t for t in mid_roster["oa_tickets"] if t["ticket_id"] == "OA-OFF-2026-1103"
            )
            assert mid_off_ticket["status"] == "blocked"
            assert mid_off_ticket["details"]["blocking_items"] == ["it_asset"]
            assert mid_off_ticket["details"]["pending_items"] == ["account_access"]
            assert mid_off_ticket["details"]["can_issue_certificate"] is False

            # XC1003 步骤二：归还 IT 设备 -> 无 blocked，但 account_access 仍为 pending。
            u2_res = await hr.call_tool(
                "hroa_update_clearance_item",
                {
                    "employee_no": "XC1003",
                    "employee_name": "韩景明",
                    "item_key": "it_asset",
                    "new_status": "cleared",
                    "evidence_note": "已验收归还 IT-MBP-2023-088（序列号 XC-SN-88412）",
                },
            )
            assert u2_res.is_error is False
            u2 = cast(dict[str, Any], u2_res.structured_content)
            assert u2["blocking_items"] == []
            assert u2["pending_items"] == ["account_access"]
            assert u2["can_issue_certificate"] is False

            # XC1003 步骤三：最后工作日 18:00 回收全部权限 -> 4 项全 cleared，可开离职证明。
            u3_res = await hr.call_tool(
                "hroa_update_clearance_item",
                {
                    "employee_no": "XC1003",
                    "employee_name": "韩景明",
                    "item_key": "account_access",
                    "new_status": "cleared",
                    "evidence_note": "已于最后工作日 18:00 吊销域账号、VPN、GitLab 与堡垒机",
                },
            )
            assert u3_res.is_error is False
            u3 = cast(dict[str, Any], u3_res.structured_content)
            assert u3["blocking_items"] == []
            assert u3["pending_items"] == []
            assert u3["can_issue_certificate"] is True

        # 3. 再次启动新的 standard 子进程回读跨进程持久化结果。
        async with _hroa_client("standard", state_file) as std_again:
            bal_after = cast(
                dict[str, Any],
                (
                    await std_again.call_tool("hroa_get_leave_balance", {"employee_no": "MOCK005"})
                ).structured_content,
            )
            assert bal_after["annual_remaining_days"] == 3.0

            tickets_res = cast(
                dict[str, Any],
                (
                    await std_again.call_tool(
                        "hroa_list_oa_tickets",
                        {"ticket_type": "leave", "employee_no": "MOCK005"},
                    )
                ).structured_content,
            )
            assert any(t["ticket_id"] == new_leave_ticket_id for t in tickets_res["tickets"])

            clr_after = cast(
                dict[str, Any],
                (
                    await std_again.call_tool(
                        "hroa_check_offboarding_clearance", {"employee_no": "XC1003"}
                    )
                ).structured_content,
            )
            assert clr_after["can_issue_certificate"] is True

            # 验证通过 standard Profile 的 hroa_list_oa_tickets 也能读到同步后的 details 明细。
            off_tickets_res = cast(
                dict[str, Any],
                (
                    await std_again.call_tool(
                        "hroa_list_oa_tickets",
                        {"ticket_type": "offboarding", "employee_no": "XC1003"},
                    )
                ).structured_content,
            )
            xc1003_ticket = next(
                t for t in off_tickets_res["tickets"] if t["ticket_id"] == "OA-OFF-2026-1103"
            )
            assert xc1003_ticket["status"] == "ready_for_certificate"
            assert xc1003_ticket["details"]["blocking_items"] == []
            assert xc1003_ticket["details"]["pending_items"] == []
            assert xc1003_ticket["details"]["can_issue_certificate"] is True

    asyncio.run(scenario())

    # 4. 执行 --reset-state 命令行复位并确认一切回到 2026-11-24 初始基线。
    reset_proc = subprocess.run(
        [sys.executable, str(HROA_SERVER_PATH), "--state-file", str(state_file), "--reset-state"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert reset_proc.returncode == 0, reset_proc.stderr

    async def verify_reset() -> None:
        async with _hroa_client("standard", state_file) as std_reset:
            bal = cast(
                dict[str, Any],
                (
                    await std_reset.call_tool("hroa_get_leave_balance", {"employee_no": "MOCK005"})
                ).structured_content,
            )
            assert bal["annual_remaining_days"] == 4.5

            clr = cast(
                dict[str, Any],
                (
                    await std_reset.call_tool(
                        "hroa_check_offboarding_clearance", {"employee_no": "XC1003"}
                    )
                ).structured_content,
            )
            assert clr["can_issue_certificate"] is False
            assert set(clr["blocking_items"]) == {"it_asset", "finance_advance"}

    asyncio.run(verify_reset())


def test_hroa_negative_business_validations(tmp_path: Path) -> None:
    """校验已离职员工拒单、超额请假拒单并给拆假建议以及非法 0.3 天步长拦截。"""
    state_file = tmp_path / "neg-state.json"

    async def scenario() -> None:
        async with _hroa_client("standard", state_file) as std:
            # 1. 已离职员工 XC1006 禁止请假与调岗。
            resigned_leave = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "XC1006",
                    "employee_name": "叶舒然",
                    "leave_type": "annual",
                    "start_date": "2026-12-01",
                    "end_date": "2026-12-01",
                    "days": 1.0,
                    "reason": "负样本测试",
                    "handover_to": "MOCK006",
                },
            )
            assert resigned_leave.is_error is True
            assert "employee_already_resigned" in _error_text(resigned_leave)

            resigned_trf = await std.call_tool(
                "hroa_submit_transfer_request",
                {
                    "employee_no": "XC1006",
                    "employee_name": "叶舒然",
                    "change_type": "cross_dept_transfer",
                    "target_department_id": "dept_mock_fin",
                    "target_department_name": "财务部",
                    "target_title": "会计",
                    "effective_date": "2026-12-01",
                    "reason": "负样本测试",
                },
            )
            assert resigned_trf.is_error is True
            assert "employee_already_resigned" in _error_text(resigned_trf)

            # 2. XC1005 余额不足（剩 2.0 年假 + 0.5 调休，申请 3.0 天年假）必须拒单并给出拆假建议。
            over_leave = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "XC1005",
                    "employee_name": "高明轩",
                    "leave_type": "annual",
                    "start_date": "2026-12-02",
                    "end_date": "2026-12-04",
                    "days": 3.0,
                    "reason": "探亲休假",
                    "handover_to": "MOCK003",
                },
            )
            assert over_leave.is_error is True
            over_err = _error_text(over_leave)
            assert "leave_balance_insufficient" in over_err
            assert "年假 2 天 + 调休 0.5 天 + 事假 0.5 天" in over_err

            # 3. 非 0.5 整数倍步长（0.3 天）必须明确拒绝。
            bad_step = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "MOCK005",
                    "employee_name": "陆知微",
                    "leave_type": "annual",
                    "start_date": "2026-12-02",
                    "end_date": "2026-12-02",
                    "days": 0.3,
                    "reason": "非法步长测试",
                    "handover_to": "MOCK003",
                },
            )
            assert bad_step.is_error is True
            assert "leave_days_invalid" in _error_text(bad_step)

            # 4. 待入职员工 XC1001 尚未正式报到，禁止请假。
            preboarding_leave = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "XC1001",
                    "employee_name": "陈思远",
                    "leave_type": "personal",
                    "start_date": "2026-12-02",
                    "end_date": "2026-12-02",
                    "days": 1.0,
                    "reason": "待入职请假负样本",
                    "handover_to": "MOCK005",
                },
            )
            assert preboarding_leave.is_error is True
            assert "employee_not_onboarded" in _error_text(preboarding_leave)

            # 5. 试用期员工 XC1002 当年折算年假处于冻结状态，禁止申请年假（允许请病假/事假）。
            probation_annual = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "XC1002",
                    "employee_name": "许知夏",
                    "leave_type": "annual",
                    "start_date": "2026-11-26",
                    "end_date": "2026-11-26",
                    "days": 1.0,
                    "reason": "试用期年假负样本",
                    "handover_to": "MOCK003",
                },
            )
            assert probation_annual.is_error is True
            assert "probation_annual_leave_frozen" in _error_text(probation_annual)

            # 6. 申请天数超过日期区间自然日跨度（1 天跨度申请 2.0 天）必须拒绝。
            exceed_span = await std.call_tool(
                "hroa_submit_leave_request",
                {
                    "employee_no": "MOCK005",
                    "employee_name": "陆知微",
                    "leave_type": "annual",
                    "start_date": "2026-12-02",
                    "end_date": "2026-12-02",
                    "days": 2.0,
                    "reason": "天数超区间负样本",
                    "handover_to": "MOCK003",
                },
            )
            assert exceed_span.is_error is True
            assert "leave_days_exceed_date_range" in _error_text(exceed_span)

        async with _hroa_client("hr_ops", state_file) as hr:
            # 7. 已离职员工 XC1006 禁止重复发起离职，也禁止修改其已封存的历史清查记录。
            resigned_off = await hr.call_tool(
                "hroa_initiate_offboarding",
                {
                    "employee_no": "XC1006",
                    "employee_name": "叶舒然",
                    "last_working_day": "2026-12-10",
                    "leave_reason_type": "重复离职负样本",
                    "handover_to": "MOCK006",
                },
            )
            assert resigned_off.is_error is True
            assert "employee_already_resigned" in _error_text(resigned_off)

            resigned_clearance_mut = await hr.call_tool(
                "hroa_update_clearance_item",
                {
                    "employee_no": "XC1006",
                    "employee_name": "叶舒然",
                    "item_key": "it_asset",
                    "new_status": "blocked",
                    "evidence_note": "尝试篡改已离职历史清查记录",
                },
            )
            assert resigned_clearance_mut.is_error is True
            assert "employee_already_resigned" in _error_text(resigned_clearance_mut)

    asyncio.run(scenario())


def test_hroa_v2_six_new_tools_end_to_end_persistence_and_reset(tmp_path: Path) -> None:
    """覆盖 V2 新增 6 个工具跨三 Profile 状态持久化与 --reset-state 基线复位。"""
    state_file = tmp_path / "hroa-v2-state.json"

    async def scenario() -> None:
        # 1. standard Profile：查询绩效基线、提交三档合同用印与 IT 工单。
        async with _hroa_client("standard", state_file) as std:
            perf_xc1004 = cast(
                dict[str, Any],
                (
                    await std.call_tool("hroa_get_performance_review", {"employee_no": "XC1004"})
                ).structured_content,
            )
            assert perf_xc1004["rating"] == "A"
            assert perf_xc1004["pip_required"] is False

            perf_xc1007 = cast(
                dict[str, Any],
                (
                    await std.call_tool("hroa_get_performance_review", {"employee_no": "XC1007"})
                ).structured_content,
            )
            assert perf_xc1007["rating"] == "S"

            perf_xc1008 = cast(
                dict[str, Any],
                (
                    await std.call_tool("hroa_get_performance_review", {"employee_no": "XC1008"})
                ).structured_content,
            )
            assert perf_xc1008["rating"] == "C"
            assert perf_xc1008["pip_required"] is True
            assert perf_xc1008["pip_days"] == 60
            assert perf_xc1008["appeal_deadline"] == "2026-12-02"

            perf_xc1002 = cast(
                dict[str, Any],
                (
                    await std.call_tool("hroa_get_performance_review", {"employee_no": "XC1002"})
                ).structured_content,
            )
            assert perf_xc1002["status"] == "not_applicable_probation"
            assert perf_xc1002["eligible"] is False

            # 三档合同用印审批链自动路由验证。
            seal_t1 = cast(
                dict[str, Any],
                (
                    await std.call_tool(
                        "hroa_submit_contract_seal_request",
                        {
                            "applicant_no": "MOCK005",
                            "applicant_name": "陆知微",
                            "contract_no": "CTR-2026-1201",
                            "contract_title": "域名与证书续费协议",
                            "counterparty": "星云证书服务有限公司",
                            "amount_cny": 68000.0,
                            "seal_type": "合同专用章",
                            "legal_review_code": "LEG-REV-2026-0501",
                        },
                    )
                ).structured_content,
            )
            assert seal_t1["approval_tier"] == "tier_1_dept_and_legal"

            seal_t2 = cast(
                dict[str, Any],
                (
                    await std.call_tool(
                        "hroa_submit_contract_seal_request",
                        {
                            "applicant_no": "MOCK005",
                            "applicant_name": "陆知微",
                            "contract_no": "CTR-2026-1120",
                            "contract_title": "星辰智联云算力年度采购框架合同",
                            "counterparty": "云枢算力科技有限公司",
                            "amount_cny": 360000.0,
                            "seal_type": "合同专用章",
                            "legal_review_code": "LEG-REV-2026-0418",
                        },
                    )
                ).structured_content,
            )
            assert seal_t2["approval_tier"] == "tier_2_finance_countersign"
            assert any("林清岚" in step for step in seal_t2["approval_chain"])

            seal_t3 = cast(
                dict[str, Any],
                (
                    await std.call_tool(
                        "hroa_submit_contract_seal_request",
                        {
                            "applicant_no": "MOCK003",
                            "applicant_name": "顾行知",
                            "contract_no": "CTR-2026-1203",
                            "contract_title": "核心推理卡独家排他供应协议",
                            "counterparty": "云枢算力科技有限公司",
                            "amount_cny": 95000.0,
                            "seal_type": "公司公章",
                            "legal_review_code": "LEG-REV-2026-0503",
                            "special_clause_flag": True,
                        },
                    )
                ).structured_content,
            )
            assert seal_t3["approval_tier"] == "tier_3_group_head_final"
            assert any("周明远" in step for step in seal_t3["approval_chain"])

            # 提交 P1 IT 工单并验证 SLA 自动标注。
            it_res = cast(
                dict[str, Any],
                (
                    await std.call_tool(
                        "hroa_submit_it_asset_request",
                        {
                            "employee_no": "XC1004",
                            "employee_name": "郑语桐",
                            "request_type": "permission_revocation",
                            "item_spec": "回收财务部 ERP 审批权并开通 HR 薪酬只读权限",
                            "priority": "P1",
                            "justification": "2026-12-01 跨部门平调权限隔离",
                        },
                    )
                ).structured_content,
            )
            assert it_res["priority"] == "P1"
            assert it_res["sla"]["response_sla"] == "1 小时内响应"

        # 2. hr_ops Profile：对 XC1008 执行申诉复议调为 B 并解除 PIP。
        async with _hroa_client("hr_ops", state_file) as hr:
            cal_res = await hr.call_tool(
                "hroa_submit_performance_calibration",
                {
                    "employee_no": "XC1008",
                    "employee_name": "方若琳",
                    "proposed_rating": "B",
                    "action_type": "appeal_review",
                    "rationale": "独立复议小组核实 Q3-Q4 应收对账零差错，采纳申诉调整为 B",
                    "operator_no": "MOCK006",
                },
            )
            assert cal_res.is_error is False
            cal_data = cast(dict[str, Any], cal_res.structured_content)
            assert cal_data["previous_rating"] == "C"
            assert cal_data["current_rating"] == "B"
            assert cal_data["pip_required"] is False
            assert cal_data["pip_days"] == 0

        # 3. fin_ops Profile：导出 6 笔差旅单并落盘 TRV-2026-1102 超标拦截结论。
        async with _hroa_client("fin_ops", state_file) as fin:
            list_res = await fin.call_tool(
                "hroa_list_travel_expenses",
                {"month": "2026-11", "department_id": "ALL"},
            )
            assert list_res.is_error is False
            list_data = cast(dict[str, Any], list_res.structured_content)
            assert list_data["schema_version"] == 1
            assert len(list_data["expenses"]) == 6

            flag_res = await fin.call_tool(
                "hroa_flag_expense_audit_result",
                {
                    "expense_id": "TRV-2026-1102",
                    "decision": "rejected_over_limit",
                    "audit_note": "依据 XC-FIN-TRV-021，上海张江住宿 920.00/晚超标 540.00 元",
                    "auditor_no": "MOCK002",
                },
            )
            assert flag_res.is_error is False

            # 回读验证新状态跨请求立即可见。
            list_after = cast(
                dict[str, Any],
                (
                    await fin.call_tool(
                        "hroa_list_travel_expenses",
                        {"month": "2026-11", "department_id": "dept_mock_rd"},
                    )
                ).structured_content,
            )
            assert len(list_after["expenses"]) == 1
            assert list_after["expenses"][0]["audit_status"] == "rejected_over_limit"
            assert list_after["expenses"][0]["audited_by"] == "MOCK002"

        # 4. 跨进程回读 standard Profile 验证 XC1008 绩效变更持久化。
        async with _hroa_client("standard", state_file) as std_again:
            perf_after = cast(
                dict[str, Any],
                (
                    await std_again.call_tool(
                        "hroa_get_performance_review", {"employee_no": "XC1008"}
                    )
                ).structured_content,
            )
            assert perf_after["rating"] == "B"
            assert perf_after["status"] == "appeal_resolved"
            assert perf_after["pip_required"] is False

    asyncio.run(scenario())

    # 5. 执行 --reset-state 复位并验证 XC1008 恢复 C 档 PIP、TRV-2026-1102 恢复 pending_audit。
    reset_proc = subprocess.run(
        [sys.executable, str(HROA_SERVER_PATH), "--state-file", str(state_file), "--reset-state"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert reset_proc.returncode == 0, reset_proc.stderr

    async def verify_v2_reset() -> None:
        async with _hroa_client("standard", state_file) as std_reset:
            perf = cast(
                dict[str, Any],
                (
                    await std_reset.call_tool(
                        "hroa_get_performance_review", {"employee_no": "XC1008"}
                    )
                ).structured_content,
            )
            assert perf["rating"] == "C"
            assert perf["pip_required"] is True

        async with _hroa_client("fin_ops", state_file) as fin_reset:
            data = cast(
                dict[str, Any],
                (
                    await fin_reset.call_tool(
                        "hroa_list_travel_expenses",
                        {"month": "2026-11", "department_id": "dept_mock_rd"},
                    )
                ).structured_content,
            )
            assert data["expenses"][0]["audit_status"] == "pending_audit"

    asyncio.run(verify_v2_reset())


def test_hroa_v2_negative_business_guards(tmp_path: Path) -> None:
    """全面校验 V2 合同用印、IT 工单、绩效校准与差旅稽核的全部负面业务守卫。"""
    state_file = tmp_path / "hroa-v2-neg.json"

    async def scenario() -> None:
        async with _hroa_client("standard", state_file) as std:
            # 1. 空白合同用印必须拒单。
            blank_res = await std.call_tool(
                "hroa_submit_contract_seal_request",
                {
                    "applicant_no": "MOCK005",
                    "applicant_name": "陆知微",
                    "contract_no": "CTR-2026-1198",
                    "contract_title": "空白框架合同测试",
                    "counterparty": "测试公司",
                    "amount_cny": 50000.0,
                    "seal_type": "合同专用章",
                    "legal_review_code": "LEG-REV-2026-0499",
                    "is_blank_contract": True,
                },
            )
            assert blank_res.is_error is True
            assert "blank_contract_forbidden" in _error_text(blank_res)

            # 2. 缺失或非法法务初审编号（CTR-2026-1199）必须拒单。
            no_legal_res = await std.call_tool(
                "hroa_submit_contract_seal_request",
                {
                    "applicant_no": "MOCK005",
                    "applicant_name": "陆知微",
                    "contract_no": "CTR-2026-1199",
                    "contract_title": "缺失法务初审负样本",
                    "counterparty": "测试公司",
                    "amount_cny": 85000.0,
                    "seal_type": "合同专用章",
                    "legal_review_code": "",
                },
            )
            assert no_legal_res.is_error is True
            assert "legal_review_code_required" in _error_text(no_legal_res)

            # 3. 合同金额 <= 0 与已离职申请人 XC1006 必须拒单。
            bad_amt = await std.call_tool(
                "hroa_submit_contract_seal_request",
                {
                    "applicant_no": "MOCK005",
                    "applicant_name": "陆知微",
                    "contract_no": "CTR-2026-1197",
                    "contract_title": "零金额负样本",
                    "counterparty": "测试公司",
                    "amount_cny": 0.0,
                    "seal_type": "合同专用章",
                    "legal_review_code": "LEG-REV-2026-0498",
                },
            )
            assert bad_amt.is_error is True
            assert "contract_amount_invalid" in _error_text(bad_amt)

            resigned_seal = await std.call_tool(
                "hroa_submit_contract_seal_request",
                {
                    "applicant_no": "XC1006",
                    "applicant_name": "叶舒然",
                    "contract_no": "CTR-2026-1196",
                    "contract_title": "离职员工用印负样本",
                    "counterparty": "测试公司",
                    "amount_cny": 10000.0,
                    "seal_type": "合同专用章",
                    "legal_review_code": "LEG-REV-2026-0497",
                },
            )
            assert resigned_seal.is_error is True
            assert "employee_already_resigned" in _error_text(resigned_seal)

            # 4. IT 工单拦截已离职员工与非法优先级。
            bad_prio = await std.call_tool(
                "hroa_submit_it_asset_request",
                {
                    "employee_no": "MOCK005",
                    "employee_name": "陆知微",
                    "request_type": "laptop_replace",
                    "item_spec": "MacBook Pro 16",
                    "priority": "P9",
                    "justification": "非法优先级测试",
                },
            )
            assert bad_prio.is_error is True
            assert "it_priority_invalid" in _error_text(bad_prio)

            resigned_it = await std.call_tool(
                "hroa_submit_it_asset_request",
                {
                    "employee_no": "XC1006",
                    "employee_name": "叶舒然",
                    "request_type": "vpn_access",
                    "item_spec": "VPN 账号",
                    "priority": "P2",
                    "justification": "已离职员工测试",
                },
            )
            assert resigned_it.is_error is True
            assert "employee_already_resigned" in _error_text(resigned_it)

        async with _hroa_client("hr_ops", state_file) as hr:
            # 5. 绩效校准拦截已离职、试用期/待入职、非法等级与非法动作。
            for emp_no, emp_name, expected_code in (
                ("XC1006", "叶舒然", "employee_already_resigned"),
                ("XC1002", "许知夏", "performance_cycle_not_eligible"),
                ("XC1001", "陈思远", "performance_cycle_not_eligible"),
            ):
                resp = await hr.call_tool(
                    "hroa_submit_performance_calibration",
                    {
                        "employee_no": emp_no,
                        "employee_name": emp_name,
                        "proposed_rating": "B",
                        "action_type": "calibration_adjust",
                        "rationale": "边界人群负样本校验",
                        "operator_no": "MOCK006",
                    },
                )
                assert resp.is_error is True
                assert expected_code in _error_text(resp)

            bad_rating = await hr.call_tool(
                "hroa_submit_performance_calibration",
                {
                    "employee_no": "XC1008",
                    "employee_name": "方若琳",
                    "proposed_rating": "D",
                    "action_type": "calibration_adjust",
                    "rationale": "非法等级负样本",
                    "operator_no": "MOCK006",
                },
            )
            assert bad_rating.is_error is True
            assert "performance_rating_invalid" in _error_text(bad_rating)

            bad_action = await hr.call_tool(
                "hroa_submit_performance_calibration",
                {
                    "employee_no": "XC1008",
                    "employee_name": "方若琳",
                    "proposed_rating": "B",
                    "action_type": "force_override",
                    "rationale": "非法动作负样本",
                    "operator_no": "MOCK006",
                },
            )
            assert bad_action.is_error is True
            assert "performance_action_invalid" in _error_text(bad_action)

            # 5b. 非 C 档禁止执行 pip_confirm，离职冻结中员工 XC1003 禁止修改绩效。
            pip_non_c = await hr.call_tool(
                "hroa_submit_performance_calibration",
                {
                    "employee_no": "XC1008",
                    "employee_name": "方若琳",
                    "proposed_rating": "B",
                    "action_type": "pip_confirm",
                    "rationale": "尝试对非 C 档确认 PIP 负样本",
                    "operator_no": "MOCK006",
                },
            )
            assert pip_non_c.is_error is True
            assert "pip_confirm_requires_rating_c" in _error_text(pip_non_c)

            offboarding_cal = await hr.call_tool(
                "hroa_submit_performance_calibration",
                {
                    "employee_no": "XC1003",
                    "employee_name": "韩景明",
                    "proposed_rating": "A",
                    "action_type": "calibration_adjust",
                    "rationale": "尝试修改离职冻结中员工绩效负样本",
                    "operator_no": "MOCK006",
                },
            )
            assert offboarding_cal.is_error is True
            assert "performance_offboarding_frozen" in _error_text(offboarding_cal)

        async with _hroa_client("fin_ops", state_file) as fin:
            # 6. 差旅稽核拦截不存在单号、非法结论与空备注。
            bad_exp = await fin.call_tool(
                "hroa_flag_expense_audit_result",
                {
                    "expense_id": "TRV-2026-9999",
                    "decision": "approved",
                    "audit_note": "单号不存在",
                    "auditor_no": "MOCK002",
                },
            )
            assert bad_exp.is_error is True
            assert "travel_expense_not_found" in _error_text(bad_exp)

            bad_dec = await fin.call_tool(
                "hroa_flag_expense_audit_result",
                {
                    "expense_id": "TRV-2026-1102",
                    "decision": "ignore_limit",
                    "audit_note": "非法结论",
                    "auditor_no": "MOCK002",
                },
            )
            assert bad_dec.is_error is True
            assert "expense_audit_decision_invalid" in _error_text(bad_dec)

            empty_note = await fin.call_tool(
                "hroa_flag_expense_audit_result",
                {
                    "expense_id": "TRV-2026-1102",
                    "decision": "approved",
                    "audit_note": "   ",
                    "auditor_no": "MOCK002",
                },
            )
            assert empty_note.is_error is True
            assert "audit_note_required" in _error_text(empty_note)

    asyncio.run(scenario())
