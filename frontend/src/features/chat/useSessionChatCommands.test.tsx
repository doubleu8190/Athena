import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getRequestUrl, makeRun } from "../../test/fixtures";
import { jsonResponse } from "../../test/testUtils";
import { DraftChatProvider } from "./DraftChatContext";
import type {
  PendingSubagentApproval,
  PendingSubagentInput,
  Run,
  RunListResponse,
  RunPendingApproval,
} from "./model";
import { chatKeys } from "./queryKeys";
import {
  useSessionChatCommands,
  type RunSnapshotRefValue,
  type UseSessionChatCommandsOptions,
} from "./useSessionChatCommands";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={["/chat/session-cmd"]}>
        <QueryClientProvider client={queryClient}>
          <DraftChatProvider>{children}</DraftChatProvider>
        </QueryClientProvider>
      </MemoryRouter>
    );
  };
}

function createOptions(
  overrides: Partial<UseSessionChatCommandsOptions> = {},
): UseSessionChatCommandsOptions {
  const runSnapshotRef: { current: RunSnapshotRefValue } = {
    current: {
      run: overrides.run ?? null,
      status: overrides.status ?? null,
      streamRunId: null,
      streamStatus: null,
    },
  };
  return {
    activeSubagentInput: null,
    currentRunId: overrides.run?.id ?? null,
    flushReconcile: vi.fn(),
    isActive: false,
    isLatestRun: true,
    isTargetedSubagentAnswer: false,
    isWaitingInput: false,
    latestRunsIsSuccess: true,
    messages: [],
    run: null,
    runSnapshotRef,
    sessionId: "session-cmd",
    setSelectedRunId: vi.fn(),
    status: null,
    syncStreamStatus: vi.fn(),
    ...overrides,
  };
}

describe("useSessionChatCommands 命令与审批子 Hook", () => {
  it("普通提交 503 失败后相同指纹复用 request_id，变更知识库三态或 4xx 后签发新键", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const submittedBodies: Array<Record<string, unknown>> = [];
    let attempt = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        attempt += 1;
        submittedBodies.push(
          JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
        );
        if (attempt === 1) {
          return jsonResponse({ detail: "temporary_unavailable" }, 503);
        }
        if (attempt === 2) {
          return jsonResponse({ detail: "request_id_conflict" }, 409);
        }
        return jsonResponse(
          makeRun({
            id: "run-submitted",
            session_id: "session-cmd",
            status: "queued",
          }),
        );
      }),
    );

    const options = createOptions();
    const { result } = renderHook(() => useSessionChatCommands(options), {
      wrapper: createWrapper(queryClient),
    });

    // 1. 首次提交命中 503（可复用 request_id）
    let firstOk = true;
    await act(async () => {
      firstOk = await result.current.handleSubmit("汇总华东区 Q4 营收");
    });
    expect(firstOk).toBe(false);
    expect(result.current.commandError).toBeTruthy();

    // 2. 相同参数重试 -> 复用相同 request_id，本次命中 409（不可复用）
    await act(async () => {
      await result.current.handleSubmit("汇总华东区 Q4 营收");
    });
    expect(submittedBodies[1]?.request_id).toBe(submittedBodies[0]?.request_id);

    // 3. 4xx 后再次重试 -> 即使参数相同也签发全新 request_id 并成功提交
    let thirdOk = false;
    await act(async () => {
      thirdOk = await result.current.handleSubmit("汇总华东区 Q4 营收");
    });
    expect(thirdOk).toBe(true);
    expect(submittedBodies[2]?.request_id).not.toBe(
      submittedBodies[1]?.request_id,
    );
    expect(options.setSelectedRunId).toHaveBeenCalledWith("run-submitted");
    expect(options.syncStreamStatus).toHaveBeenCalledWith(
      "run-submitted",
      "queued",
    );
  });

  it("提交 503 后仅切换知识库三态（null -> []）即判定指纹变更并换新 request_id", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const submittedBodies: Array<Record<string, unknown>> = [];
    let attempt = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        attempt += 1;
        submittedBodies.push(
          JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
        );
        if (attempt === 1) {
          return jsonResponse({ detail: "temporary_unavailable" }, 503);
        }
        return jsonResponse(
          makeRun({
            id: "run-kb-changed",
            session_id: "session-cmd",
            status: "queued",
          }),
        );
      }),
    );

    const options = createOptions();
    const { result } = renderHook(() => useSessionChatCommands(options), {
      wrapper: createWrapper(queryClient),
    });

    await act(async () => {
      await result.current.handleSubmit("分析费用结构");
    });
    act(() => {
      result.current.setKnowledgeBaseIds([]);
    });
    await act(async () => {
      await result.current.handleSubmit("分析费用结构");
    });

    expect(submittedBodies[0]?.knowledge_base_ids).toBeNull();
    expect(submittedBodies[1]?.knowledge_base_ids).toEqual([]);
    expect(submittedBodies[1]?.request_id).not.toBe(
      submittedBodies[0]?.request_id,
    );
  });

  it("子智能体定向回答保留父 Run 选中态，并乐观补丁子 Run 交互与父 Run 双缓存", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const subInput: PendingSubagentInput = {
      created_at: "2026-11-10T03:00:00Z",
      input_request_id: "inp-sub-1",
      question_message_id: "msg-sub-q",
      question_text: "请确认目标年份",
      sub_run_id: "sub-run-1",
      subagent_name: "财务分析助手",
      subagent_slug: "finance",
      task_index: 1,
      task_summary: "汇总预算",
    };
    const parentRun = makeRun({
      id: "run-parent",
      pending_subagent_inputs: [subInput],
      session_id: "session-cmd",
      status: "waiting_for_subagents",
    });
    const subRunCached = makeRun({
      id: "sub-run-1",
      input_interactions: [
        {
          answer_text: null,
          answered_at: null,
          created_at: "2026-11-10T03:00:00Z",
          input_request_id: "inp-sub-1",
          question_text: "请确认目标年份",
          status: "waiting",
        },
      ],
      session_id: "session-cmd",
      status: "waiting_for_input",
    });
    queryClient.setQueryData(chatKeys.run("session-cmd", parentRun.id), parentRun);
    queryClient.setQueryData(chatKeys.latestRun("session-cmd"), {
      items: [parentRun],
      next_cursor: null,
    });
    queryClient.setQueryData(
      chatKeys.run("session-cmd", subRunCached.id),
      subRunCached,
    );

    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          makeRun({
            id: "sub-run-1",
            session_id: "session-cmd",
            status: "queued",
          }),
        ),
      ),
    );

    const options = createOptions({
      activeSubagentInput: subInput,
      currentRunId: parentRun.id,
      isActive: true,
      isTargetedSubagentAnswer: true,
      run: parentRun,
      status: "waiting_for_subagents",
    });
    const { result } = renderHook(() => useSessionChatCommands(options), {
      wrapper: createWrapper(queryClient),
    });

    let ok = false;
    await act(async () => {
      ok = await result.current.handleSubmit("2026 年");
    });

    expect(ok).toBe(true);
    // 定向子智能体回答绝不能切换父会话的 selectedRunId。
    expect(options.setSelectedRunId).not.toHaveBeenCalled();
    expect(result.current.commandNotice).toContain("财务分析助手");

    const updatedParentDetail = queryClient.getQueryData<Run>(
      chatKeys.run("session-cmd", parentRun.id),
    );
    const updatedParentLatest = queryClient.getQueryData<RunListResponse>(
      chatKeys.latestRun("session-cmd"),
    );
    expect(updatedParentDetail?.pending_subagent_inputs).toEqual([]);
    expect(updatedParentLatest?.items[0]?.pending_subagent_inputs).toEqual([]);

    const updatedSubRun = queryClient.getQueryData<Run>(
      chatKeys.run("session-cmd", "sub-run-1"),
    );
    expect(updatedSubRun?.input_interactions?.[0]).toMatchObject({
      answer_text: "2026 年",
      input_request_id: "inp-sub-1",
      status: "answered",
    });
  });

  it("主任务工具审批在同决策重试复用 request_id，变更决策时签发新键并清理错误", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const requests: Array<{ decision: string; request_id: string }> = [];
    let attempt = 0;
    const waitingRun = makeRun({
      event_sequence: 3,
      id: "run-approve",
      session_id: "session-cmd",
      status: "waiting_for_approval",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        attempt += 1;
        requests.push(
          JSON.parse(String(init?.body ?? "{}")) as {
            decision: string;
            request_id: string;
          },
        );
        if (attempt <= 2) {
          return jsonResponse({ detail: "temporary_unavailable" }, 503);
        }
        return jsonResponse({
          ...waitingRun,
          event_sequence: 4,
          status: "queued",
        });
      }),
    );

    const approval: RunPendingApproval = {
      batch_message_id: "msg-batch-1",
      call_id: "call-1",
      model_name: "expense_submit",
      ordinal: 1,
      requested_at: "2026-11-10T03:00:00Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    const options = createOptions({
      currentRunId: waitingRun.id,
      isActive: true,
      run: waitingRun,
      status: "waiting_for_approval",
    });
    const { result } = renderHook(() => useSessionChatCommands(options), {
      wrapper: createWrapper(queryClient),
    });

    await act(async () => {
      await result.current.handleApproval(approval, "approve");
    });
    expect(result.current.approvalErrors["call-1"]).toBeTruthy();

    // 同决策重试：复用同一 request_id
    await act(async () => {
      await result.current.handleApproval(approval, "approve");
    });
    expect(requests[1]?.request_id).toBe(requests[0]?.request_id);

    // 改为 reject：签发新 request_id，成功后清空错误
    await act(async () => {
      await result.current.handleApproval(approval, "reject");
    });
    expect(requests[2]?.decision).toBe("reject");
    expect(requests[2]?.request_id).not.toBe(requests[1]?.request_id);
    expect(result.current.approvalErrors["call-1"]).toBeUndefined();
    expect(options.syncStreamStatus).toHaveBeenCalledWith(
      "run-approve",
      "queued",
    );
  });

  it("子智能体审批支持多子任务并行执行且同 key 连点受锁保护，成功后同步移除父缓存待审批项", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const approvalA: PendingSubagentApproval = {
      batch_message_id: "msg-sub-a",
      call_id: "call-a",
      model_name: "tool_a",
      ordinal: 1,
      requested_at: "2026-11-10T03:00:00Z",
      safe_skill_projection: null,
      sub_run_id: "sub-a",
      subagent_name: "子任务 A",
      subagent_slug: "sub-a",
      task_index: 1,
      task_summary: "任务 A",
      tool_kind: "mcp",
    };
    const approvalB: PendingSubagentApproval = {
      ...approvalA,
      batch_message_id: "msg-sub-b",
      call_id: "call-b",
      model_name: "tool_b",
      sub_run_id: "sub-b",
      subagent_name: "子任务 B",
      subagent_slug: "sub-b",
      task_index: 2,
      task_summary: "任务 B",
    };
    const parentRun = makeRun({
      id: "run-parent-sub",
      pending_subagent_approvals: [approvalA, approvalB],
      session_id: "session-cmd",
      status: "waiting_for_subagents",
    });
    queryClient.setQueryData(chatKeys.run("session-cmd", parentRun.id), parentRun);
    queryClient.setQueryData(chatKeys.latestRun("session-cmd"), {
      items: [parentRun],
      next_cursor: null,
    });

    const gateA = deferred<Response>();
    const gateB = deferred<Response>();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = getRequestUrl(input);
      if (url.includes("/runs/sub-a/")) {
        return gateA.promise;
      }
      return gateB.promise;
    });
    vi.stubGlobal("fetch", fetchMock);

    const options = createOptions({
      currentRunId: parentRun.id,
      isActive: true,
      run: parentRun,
      status: "waiting_for_subagents",
    });
    const { result } = renderHook(() => useSessionChatCommands(options), {
      wrapper: createWrapper(queryClient),
    });

    // 同帧并发触发 A（两次连点）与 B：A 去重仅发 1 次，B 并行不阻塞。
    let pendingA1!: Promise<void>;
    let pendingA2!: Promise<void>;
    let pendingB!: Promise<void>;
    act(() => {
      pendingA1 = result.current.handleSubagentApproval(approvalA, "approve");
      pendingA2 = result.current.handleSubagentApproval(approvalA, "approve");
      pendingB = result.current.handleSubagentApproval(approvalB, "reject");
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.busySubagentApprovalKeys.size).toBe(2);

    await act(async () => {
      gateA.resolve(
        jsonResponse(
          makeRun({ id: "sub-a", session_id: "session-cmd", status: "queued" }),
        ),
      );
      gateB.resolve(
        jsonResponse(
          makeRun({ id: "sub-b", session_id: "session-cmd", status: "queued" }),
        ),
      );
      await Promise.all([pendingA1, pendingA2, pendingB]);
    });

    expect(result.current.busySubagentApprovalKeys.size).toBe(0);
    const updatedParent = queryClient.getQueryData<Run>(
      chatKeys.run("session-cmd", parentRun.id),
    );
    const updatedLatest = queryClient.getQueryData<RunListResponse>(
      chatKeys.latestRun("session-cmd"),
    );
    expect(updatedParent?.pending_subagent_approvals).toEqual([]);
    expect(updatedLatest?.items[0]?.pending_subagent_approvals).toEqual([]);
  });

  it("失败重试复用 request_id，且当取消在途时共享命令锁阻止并发提交与审批", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const failedRun = makeRun({
      event_sequence: 5,
      id: "run-failed-1",
      session_id: "session-cmd",
      status: "failed",
    });
    const retryRequestIds: string[] = [];
    let retryAttempt = 0;
    const cancelGate = deferred<Response>();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = getRequestUrl(input);
      if (url.endsWith("/retry")) {
        retryAttempt += 1;
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          request_id: string;
        };
        retryRequestIds.push(body.request_id);
        if (retryAttempt === 1) {
          return jsonResponse({ detail: "temporary_unavailable" }, 503);
        }
        return jsonResponse(
          makeRun({
            id: "run-retried-2",
            retry_of_run_id: failedRun.id,
            session_id: "session-cmd",
            status: "queued",
          }),
        );
      }
      if (url.endsWith("/cancel")) {
        return cancelGate.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const options = createOptions({
      currentRunId: failedRun.id,
      isActive: false,
      isLatestRun: true,
      run: failedRun,
      status: "failed",
    });
    const { result, rerender } = renderHook(
      (props: UseSessionChatCommandsOptions) => useSessionChatCommands(props),
      {
        initialProps: options,
        wrapper: createWrapper(queryClient),
      },
    );

    act(() => {
      result.current.openRetryDialog();
    });
    expect(result.current.retryOpen).toBe(true);

    // 首次重试失败 -> 保留弹窗与错误并记录 request_id
    await act(async () => {
      await result.current.handleRetry();
    });
    expect(result.current.retryError).toBeTruthy();
    expect(result.current.retryOpen).toBe(true);

    // 二次重试 -> 复用同一 request_id，成功后关闭弹窗并切换新 Run
    await act(async () => {
      await result.current.handleRetry();
    });
    expect(retryRequestIds[1]).toBe(retryRequestIds[0]);
    expect(result.current.retryOpen).toBe(false);
    expect(options.setSelectedRunId).toHaveBeenCalledWith("run-retried-2");

    // 切换至 running 并验证取消命令在途时共享锁阻塞后续提交
    const runningRun = makeRun({
      id: "run-retried-2",
      session_id: "session-cmd",
      status: "running",
    });
    rerender(
      createOptions({
        currentRunId: runningRun.id,
        isActive: true,
        run: runningRun,
        status: "running",
      }),
    );

    let cancelPromise!: Promise<void>;
    act(() => {
      cancelPromise = result.current.handleCancel();
    });
    expect(result.current.stopping).toBe(true);
    expect(result.current.commandPending).toBe(true);

    let blockedSubmit = true;
    await act(async () => {
      blockedSubmit = await result.current.handleSubmit("取消在途时的并发消息");
    });
    expect(blockedSubmit).toBe(false);

    await act(async () => {
      cancelGate.resolve(
        jsonResponse({
          ...runningRun,
          event_sequence: 2,
          status: "cancelling",
        }),
      );
      await cancelPromise;
    });
    expect(result.current.stopping).toBe(false);
    expect(result.current.commandPending).toBe(false);
  });
});
