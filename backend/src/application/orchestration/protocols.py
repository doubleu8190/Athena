"""编排用例与运行时适配器之间由应用层拥有的契约。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, AsyncContextManager, Protocol

from domain.leases import LeaseGrant
from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    Plan,
    ReviewContext,
    ReviewDecision,
    Task,
    TaskExecution,
    TaskResult,
)
from domain.runs import RoundCompletedResumeValue
from .root_decision import (
    RootAgentInput,
    RootDecision,
    RootPlanInput,
    RootPlanResult,
    RootReDecision,
    RootWorkerTool,
)

if TYPE_CHECKING:
    from application.orchestration.langgraph.graphs.state import RootGraphState


class RootAgent(Protocol):
    """Root 工作流所需的 Agent 能力。"""

    async def decide(self, inputs: RootAgentInput) -> RootDecision:
        """根据已校验输入生成 Root 决策。

        参数：
        - `inputs`（RootAgentInput）：经过边界校验的工作流输入，包含本次处理所需资料；约束：无额外运行时限制。

        返回：
        - RootDecision：Root 对当前请求选择的动作及其结构化参数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def redecide(self, inputs: RootAgentInput) -> RootReDecision:
        """根据新增结果或校验反馈重新生成 Root 决策。

        参数：
        - `inputs`（RootAgentInput）：经过边界校验的工作流输入，包含本次处理所需资料；约束：无额外运行时限制。

        返回：
        - RootReDecision：Root 根据检索结果和反馈生成的后续动作。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def plan(self, inputs: RootPlanInput) -> RootPlanResult:
        """根据已校验请求生成执行计划。

        参数：
        - `inputs`（RootPlanInput）：经过边界校验的工作流输入，包含本次处理所需资料；约束：无额外运行时限制。

        返回：
        - RootPlanResult：Root 生成的计划草稿或需要补充的信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def review(
        self,
        context: ReviewContext,
        *,
        worker_tools: tuple[RootWorkerTool, ...],
        request: Mapping[str, Any],
    ) -> ReviewDecision:
        """根据固定轮次上下文生成审查决策。

        参数：
        - `context`（ReviewContext）：供决策或生成步骤使用的上下文资料；约束：无额外运行时限制。
        - `worker_tools`（tuple[RootWorkerTool, ...]）：当前系统允许 Worker 调用的工具集合；约束：无额外运行时限制。
        - `request`（Mapping[str, Any]）：当前请求及其关联身份，用于限定操作范围；约束：无额外运行时限制。

        返回：
        - ReviewDecision：Root 对已完成 Round 选择的继续、修订或结束动作。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class RootGraphPort(Protocol):
    """由 LangGraph 适配器实现的 Root 图执行边界。
    """

    async def invoke(
        self,
        *,
        session_id: str,
        run_id: str,
        lease: LeaseGrant,
        user_message: str,
        message_id: str = "",
        run_sequence: int = 1,
        attachment_ids: list[str] | None = None,
        resume_value: RoundCompletedResumeValue | None = None,
        stop_signal: asyncio.Event | None = None,
    ) -> object:
        """执行一次绑定 Run 身份的 Root 图调用。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。
        - `user_message`（str）：本次 Run 对应的用户原始消息；约束：无额外运行时限制。
        - `message_id`（str）：关联消息的稳定标识；None 表示未绑定消息；约束：可省略，默认值为 `''`。
        - `run_sequence`（int）：会话内 Run 的单调递增序号；约束：可省略，默认值为 `1`。
        - `attachment_ids`（list[str] | None）：当前请求已授权使用的附件标识集合；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `resume_value`（RoundCompletedResumeValue | None）：图中断后用于恢复执行的持久化值；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `stop_signal`（asyncio.Event | None）：通知当前执行应停止的异步信号；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - object：由 Root 图调用生成的状态映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def has_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool:
        """确认检查点是否仍等待指定 Round 的完成事件。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `round_id`（str）：目标执行轮次的稳定标识；约束：无额外运行时限制。
        - `plan_version`（int）：目标计划版本号，用于固定读取或恢复的版本；约束：无额外运行时限制。
        - `interrupt_id`（str）：目标图中断的稳定标识；约束：无额外运行时限制。
        - `checkpoint_id`（str）：目标 LangGraph 检查点的稳定标识；约束：无额外运行时限制。

        返回：
        - bool：操作是否成功或条件是否成立。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def find_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
    ) -> tuple[str, str] | None:
        """查找指定 Run、Plan 和 Round 对应的未恢复等待检查点。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `round_id`（str）：目标执行轮次的稳定标识；约束：无额外运行时限制。
        - `plan_version`（int）：目标计划版本号，用于固定读取或恢复的版本；约束：无额外运行时限制。

        返回：
        - tuple[str, str] | None：检查点标识和中断标识；没有符合身份的等待时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class WorkerExecutor(Protocol):
    """持久化 Worker 作业分发器使用的应用执行能力。
    """

    async def execute(self, execution: TaskExecution) -> TaskResult:
        """执行持久化 Worker 任务。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - TaskResult：Worker 任务的最终状态、输出内容及失败信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def resume(
        self,
        execution: TaskExecution,
        decisions: dict[str, str],
    ) -> TaskResult:
        """按审批决定恢复指定 Worker 执行。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `decisions`（dict[str, str]）：按工具调用标识记录的审批决定集合；约束：无额外运行时限制。

        返回：
        - TaskResult：Worker 任务的最终状态、输出内容及失败信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def pending_interrupt(
        self, execution: TaskExecution
    ) -> dict[str, Any] | None:
        """读取已持久化但尚未关联审批行的中断。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - dict[str, Any] | None：尚未登记的 Worker 审批中断内容；没有待处理中断时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class GraphThreadLock(Protocol):
    """串行化同一检查点线程上的图调用。
    """

    def hold(self, thread_id: str) -> AsyncContextManager[None]:
        """阻止同一检查点线程上的并发图调用。

        参数：
        - `thread_id`（str）：检查点线程的稳定标识；约束：无额外运行时限制。

        返回：
        - AsyncContextManager[None]：退出时释放同一检查点线程的串行调用锁。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class PlanLoader(Protocol):
    """为 Root 工作流读取已持久化的计划状态和结果。
    """

    async def __call__(self, *, plan_id: str | None) -> Mapping[str, Any]:
        """按计划身份读取执行状态和结果。

        参数：
        - `plan_id`（str | None）：目标计划的稳定标识；约束：允许为 None，按未提供值的分支处理。

        返回：
        - Mapping[str, Any]：由计划加载器提供的执行快照字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class RootPlanCommandPort(Protocol):
    """Root 提交和审查计划时使用的应用命令端口。
    """

    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
        *,
        legal_tools: Iterable[str] | None = None,
    ) -> CommandReceipt:
        """校验并提交新的计划和任务集合。

        参数：
        - `command`（OrchestrationCommand）：领域命令，携带幂等身份和目标对象；约束：无额外运行时限制。
        - `plan`（Plan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。
        - `tasks`（list[Task]）：计划中定义的 Worker 任务集合；约束：无额外运行时限制。
        - `legal_tools`（Iterable[str] | None）：计划任务允许调用的工具名称集合；None 表示由上游规则确定；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - CommandReceipt：命令处理结果及其幂等标识、持久化状态。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def apply_review_decision(
        self, command: OrchestrationCommand, decision: ReviewDecision
    ) -> CommandReceipt:
        """将审查决策转换为幂等领域命令并提交。

        参数：
        - `command`（OrchestrationCommand）：领域命令，携带幂等身份和目标对象；约束：无额外运行时限制。
        - `decision`（ReviewDecision）：经过校验的决策内容，用于驱动后续状态转换；约束：无额外运行时限制。

        返回：
        - CommandReceipt：命令处理结果及其幂等标识、持久化状态。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...

    async def load_review_decision(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        plan_id: str,
        plan_version: int,
        round_id: str,
    ) -> ReviewDecision | None:
        """按幂等身份读取已持久化的审查决策。

        参数：
        - `scope_key`（str）：限定幂等命令作用范围的稳定键；约束：无额外运行时限制。
        - `idempotency_key`（str）：同一业务命令重试时保持不变的幂等键；约束：无额外运行时限制。
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `plan_version`（int）：目标计划版本号，用于固定读取或恢复的版本；约束：无额外运行时限制。
        - `round_id`（str）：目标执行轮次的稳定标识；约束：无额外运行时限制。

        返回：
        - ReviewDecision | None：Root 对已完成 Round 选择的继续、修订或结束动作。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class RootWorkflowPort(Protocol):
    """Root LangGraph 节点调用的应用工作流端口。
    """
    async def prepare_request(self, state: RootGraphState) -> dict[str, Any]:
        """准备请求。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Root 请求准备后新增或更新的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def prefetch_context(self, state: RootGraphState) -> dict[str, Any]:
        """预取上下文。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Root 预取上下文后新增或更新的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def decide_root_action(self, state: RootGraphState) -> dict[str, Any]:
        """根据 Root 状态生成初始决策并返回状态补丁。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Root 初始决策及其后续路由所需的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def redecide_root_action(self, state: RootGraphState) -> dict[str, Any]:
        """根据新增检索结果重新生成决策并返回状态补丁。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Root 再决策及其后续路由所需的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def acquire_context(self, state: RootGraphState) -> dict[str, Any]:
        """获取上下文。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：上下文检索和预算选择结果对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def generate_plan(self, state: RootGraphState) -> dict[str, Any]:
        """根据 Root 状态生成计划并返回状态补丁。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：计划草稿、补充信息或失败原因对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def generate_answer(self, state: RootGraphState) -> dict[str, Any]:
        """根据 Root 状态生成面向用户的回答并返回状态补丁。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：生成回答、消息标识和完成状态对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def materialize_execution_plan(
        self, state: RootGraphState
    ) -> dict[str, Any]:
        """将已校验的计划草稿持久化为可执行计划。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：持久化计划标识、版本和状态对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def wait_for_plan_completion(
        self, state: RootGraphState
    ) -> dict[str, Any]:
        """读取 Worker Round 的完成结果并返回 Root 恢复状态。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：轮次等待状态、完成结果或恢复身份对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def review_completed_round(self, state: RootGraphState) -> dict[str, Any]:
        """审查已完成的 Worker Round 并更新 Root 决策状态。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：审查决策和下一步路由信息对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def build_plan_response(self, state: RootGraphState) -> dict[str, Any]:
        """根据计划执行结果构造返回给 Root 的状态补丁。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：计划执行结果及其面向 Root 的摘要字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def assemble_final_response(
        self, state: RootGraphState
    ) -> dict[str, Any]:
        """整理最终回答、运行状态和错误信息。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：最终回答、Run 状态和错误信息对应的图状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class WorkerWorkflowPort(Protocol):
    """Worker LangGraph 节点调用的应用工作流端口。
    """
    async def prepare(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """校验执行身份并准备 Worker 图状态。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Worker 执行身份和本轮初始状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def run(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """调用 Worker Agent 并生成回答或工具调用状态。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Worker 回答正文或工具调用状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def approval_gate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """根据审批状态决定 Worker 是否可以继续。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：审批结论和等待恢复信息对应的 Worker 状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def execute_tool_batch(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """执行已通过审批的工具调用批次并收集结果。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：工具调用回执及更新后的 Worker 状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def complete_tool_approval(
        self, state: Mapping[str, Any], decision: Any
    ) -> dict[str, Any]:
        """应用审批决定并更新 Worker 工具调用状态。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
        - `decision`（Any）：经过校验的决策内容，用于驱动后续状态转换；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：审批决定、工具结果和后续路由对应的 Worker 状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    async def finalize(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """持久化 Worker 最终结果并完成任务状态。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：Worker 最终结果、任务状态和错误信息对应的状态字段。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    def route_after_gate(self, state: Mapping[str, Any]) -> str:
        """根据审批门结果选择 Worker 后续节点。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - str：Worker 图中表示审批门后续节点名称的路由键。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...
    def route_after_tools(self, state: Mapping[str, Any]) -> str:
        """根据工具执行结果选择 Worker 后续节点。

        参数：
        - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - str：Worker 图中表示工具执行后续节点名称的路由键。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


__all__ = [
    "RootAgent",
    "PlanLoader",
    "RootGraphPort",
    "WorkerExecutor",
    "GraphThreadLock",
    "RootPlanCommandPort",
    "RootWorkflowPort",
    "WorkerWorkflowPort",
]
