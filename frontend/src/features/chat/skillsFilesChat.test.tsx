import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { makeSkill } from "../../test/adminFixtures";
import {
  FakeSseStream,
  createSseStreamResponse,
} from "../../test/fakeSseStream";
import {
  getRequestUrl,
  listResponse,
  makeArtifact,
  makeMessage,
  makeRun,
  makeSession,
  makeSessionFile,
  makeToolCall,
  makeToolCallDetail,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type {
  Run,
  RunToolCallDetail,
  RunToolCallSummary,
  SafeSkillApprovalProjection,
} from "./model";

const SESSION = makeSession({
  id: "session-skills-files",
  title: "技能与文件会话",
});
const SKILL = makeSkill({
  description: "按销售明细生成报表。",
  enabled: true,
  id: "skill-sales",
  name: "销售报表",
});

const PROJECTION: SafeSkillApprovalProjection = {
  call_id: "call-skill",
  can_approve: true,
  hidden_parameter_count: 1,
  input_files: [
    {
      available: true,
      file_id: "file-input",
      filename: "sales.csv",
      mime_type: "text/csv",
      size_bytes: 1024,
    },
  ],
  parameter_summary: [
    {
      label: "统计月份",
      name: "month",
      value: "2026-11",
    },
  ],
  purpose: "根据已授权销售明细生成报表。",
  script_label: "生成销售报表",
  skill_id: SKILL.id,
  skill_name: SKILL.name,
  skill_version_id: "skill-version-1",
  skill_version_label: "v1",
  unavailable_reason: null,
};

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

async function openSkillPicker(
  user: ReturnType<typeof userEvent.setup>,
) {
  const trigger = await screen.findByRole("button", { name: /本次技能/ });
  await user.click(trigger);
  return screen.getByRole("dialog", { name: "选择本次技能" });
}

async function selectSkill(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
) {
  const dialog = await openSkillPicker(user);
  await user.click(
    within(dialog).getByRole("checkbox", {
      name: new RegExp(name),
    }),
  );
  await user.click(within(dialog).getByRole("button", { name: "完成" }));
}

interface SessionFetchOptions {
  onCreateSession?: () => Response;
  onUpload?: (file: File) => Response | Promise<Response>;
  onSubmit?: (
    body: Record<string, unknown>,
  ) => Response;
  onAnswer?: (
    body: Record<string, unknown>,
  ) => Response;
  runs: () => Run[];
  messages?: () => ReturnType<typeof makeMessage>[];
  files?: () => ReturnType<typeof makeSessionFile>[];
  toolCalls?: () => RunToolCallSummary[];
  toolCallDetail?: (callId: string) => RunToolCallDetail;
}

function installFetch(options: SessionFetchOptions) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      const method = init?.method ?? "GET";
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (url.pathname === "/api/v1/projects" && method === "GET") {
        return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
      }
      if (
        url.pathname === `/api/v1/projects/${PROJECT_A.id}` &&
        method === "GET"
      ) {
        return jsonResponse(PROJECT_A);
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        return jsonResponse(listResponse([], 1, 8, 0));
      }
      if (url.pathname === "/api/v1/sessions" && method === "POST") {
        return (
          options.onCreateSession?.() ?? jsonResponse(SESSION, 201)
        );
      }
      if (url.pathname === "/api/v1/skills" && method === "GET") {
        return jsonResponse({ items: [SKILL] });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}` &&
        method === "GET"
      ) {
        return jsonResponse(SESSION);
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.messages?.() ?? [],
          next_before_sequence: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.files?.() ?? [],
          next_after_file_id: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "POST"
      ) {
        const file = (init?.body as FormData).get("file") as File;
        return (
          options.onUpload?.(file) ??
          jsonResponse(
            makeSessionFile({
              filename: file.name,
              id: "file-uploaded",
              session_id: SESSION.id,
            }),
            201,
          )
        );
      }
      const downloadMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/files/([^/]+)/download$`,
      ).exec(url.pathname);
      if (downloadMatch && method === "GET") {
        return new Response("downloaded", {
          headers: {
            "Content-Disposition":
              'attachment; filename="report.xlsx"',
            "Content-Type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
          status: 200,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/runs` &&
        method === "GET"
      ) {
        const items = options.runs();
        return jsonResponse({
          items: items.slice(0, 1),
          next_cursor: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/runs` &&
        method === "POST"
      ) {
        const body = JSON.parse(String(init?.body)) as Record<
          string,
          unknown
        >;
        return (
          options.onSubmit?.(body) ??
          jsonResponse(
            makeRun({
              id: "run-created",
              session_id: SESSION.id,
              status: "queued",
            }),
            201,
          )
        );
      }
      const runDetailMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)$`,
      ).exec(url.pathname);
      if (runDetailMatch && method === "GET") {
        const runId = decodeURIComponent(runDetailMatch[1]);
        const run =
          options.runs().find((item) => item.id === runId) ??
          options.runs()[0];
        return run
          ? jsonResponse(run)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      const answerMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/input-requests/([^/]+)/answer$`,
      ).exec(url.pathname);
      if (answerMatch && method === "POST") {
        const body = JSON.parse(String(init?.body)) as Record<
          string,
          unknown
        >;
        return options.onAnswer?.(body) ?? jsonResponse(options.runs()[0]);
      }
      const toolCallsMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/tool-calls$`,
      ).exec(url.pathname);
      if (toolCallsMatch && method === "GET") {
        return jsonResponse({
          items: options.toolCalls?.() ?? [],
          next_after_ordinal: null,
        });
      }
      const toolDetailMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/tool-calls/([^/]+)$`,
      ).exec(url.pathname);
      if (toolDetailMatch && method === "GET") {
        const callId = decodeURIComponent(toolDetailMatch[2]);
        const detail = options.toolCallDetail?.(callId);
        return detail
          ? jsonResponse(detail)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      if (url.pathname.endsWith("/stream") && method === "GET") {
        return createSseStreamResponse(url.pathname, init);
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  FakeSseStream.instances = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("聊天技能选择与会话文件", () => {
  it("新会话先上传文件，再提交技能 ID 与文件 ID", async () => {
    let submitBody: Record<string, unknown> | null = null;
    let uploadedFile: File | null = null;
    const fetchMock = installFetch({
      onCreateSession: () => jsonResponse(SESSION, 201),
      onUpload: (file) => {
        uploadedFile = file;
        return jsonResponse(
          makeSessionFile({
            filename: file.name,
            id: "file-new",
            session_id: SESSION.id,
          }),
          201,
        );
      },
      onSubmit: (body) => {
        submitBody = body;
        return jsonResponse(
          makeRun({
            id: "run-created",
            session_id: SESSION.id,
            status: "queued",
          }),
          201,
        );
      },
      runs: () => [],
    });
    const user = userEvent.setup();

    renderApp("/chat");

    await user.type(
      await screen.findByLabelText("对话草稿"),
      "生成本月销售报表",
    );
    await selectSkill(user, "销售报表");
    const file = new File(["month,amount\n11,42"], "sales.csv", {
      type: "text/csv",
    });
    await user.upload(screen.getByLabelText("选择任务文件"), file);
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(submitBody).not.toBeNull());
    expect(uploadedFile).toBe(file);
    expect(submitBody).toMatchObject({
      input_file_ids: ["file-new"],
      skill_ids: [SKILL.id],
      text: "生成本月销售报表",
    });
    expect(submitBody).toHaveProperty("request_id");
    // 文件上传必须带可安全重放的请求键，服务端据此防止网络重试产生重复文件。
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname ===
            `/api/v1/sessions/${SESSION.id}/files` &&
          init?.method === "POST" &&
          Boolean(new Headers(init.headers).get("Idempotency-Key")),
      ),
    ).toBe(true);
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "技能与文件会话" }))
        .toBeInTheDocument();
    });
  });

  it("会话附件上传进行中时明确禁用发送且不会创建 Run，上传完成后自动选中可发送", async () => {
    let resolveUpload: ((response: Response) => void) | undefined;
    const fetchMock = installFetch({
      onUpload: () =>
        new Promise<Response>((resolve) => {
          resolveUpload = resolve;
        }),
      runs: () => [],
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await user.type(
      await screen.findByLabelText("对话草稿"),
      "这份草稿需要一起提交",
    );
    await user.upload(
      screen.getByLabelText("选择会话文件"),
      new File(["draft"], "pending.txt", { type: "text/plain" }),
    );

    expect(
      await screen.findByText(/正在上传附件，完成后即可发送/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "发送消息" }),
    ).toBeDisabled();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname ===
            `/api/v1/sessions/${SESSION.id}/runs` &&
          init?.method === "POST",
      ),
    ).toBe(false);

    resolveUpload?.(
      jsonResponse(
        makeSessionFile({
          filename: "pending.txt",
          id: "file-pending-done",
          session_id: SESSION.id,
        }),
        201,
      ),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled(),
    );
  });

  it("补充回答前附件上传未完成或失败时禁用回答且不会提交 Run 输入", async () => {
    const waiting = makeRun({
      id: "run-waiting-pending-upload",
      pending_input: {
        created_at: "2026-11-12T03:00:00Z",
        id: "input-pending-upload",
        question_message_id: "question-pending-upload",
        status: "pending",
      },
      session_id: SESSION.id,
      status: "waiting_for_input",
    });
    const fetchMock = installFetch({
      messages: () => [
        makeMessage({
          id: "question-pending-upload",
          parts: [
            {
              text: "请补充需要分析的文件。",
              type: "text",
            },
          ],
          sequence: 1,
          session_id: SESSION.id,
        }),
      ],
      onUpload: () =>
        jsonResponse({ detail: "file_storage_unavailable" }, 503),
      runs: () => [waiting],
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await user.type(
      await screen.findByLabelText("补充信息"),
      "使用稍后上传的文件",
    );
    await user.upload(
      screen.getByLabelText("选择会话文件"),
      new File(["draft"], "answer-pending.txt", {
        type: "text/plain",
      }),
    );

    expect(
      await screen.findByText(/有附件上传失败，请先重试或移除后再发送/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "提交回答" }),
    ).toBeDisabled();
    expect(
      fetchMock.mock.calls.some(([input, init]) =>
        requestUrl(input).pathname.endsWith(
          "/input-requests/input-pending-upload/answer",
        ) && init?.method === "POST",
      ),
    ).toBe(false);
  });

  it("上传失败后进入已创建会话并保留文字、技能选择和待上传文件", async () => {
    installFetch({
      onCreateSession: () => jsonResponse(SESSION, 201),
      onUpload: () =>
        jsonResponse({ detail: "file_storage_unavailable" }, 503),
      runs: () => [],
    });
    const user = userEvent.setup();
    let submitCalls = 0;

    const fetchMock = vi.mocked(fetch);
    renderApp("/chat");

    await user.type(await screen.findByLabelText("对话草稿"), "保留这次任务");
    await selectSkill(user, "销售报表");
    const file = new File(["draft"], "draft.txt", {
      type: "text/plain",
    });
    await user.upload(screen.getByLabelText("选择任务文件"), file);
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByRole("heading", { name: "技能与文件会话" }),
    ).toBeInTheDocument();
    expect(await screen.findByLabelText("对话草稿")).toHaveValue(
      "保留这次任务",
    );
    expect(
      screen.getByRole("button", { name: /本次技能/ }),
    ).toHaveTextContent("销售报表");
    const skillDialog = await openSkillPicker(user);
    expect(
      within(skillDialog).getByRole("checkbox", { name: /销售报表/ }),
    ).toBeChecked();
    await user.click(
      within(skillDialog).getByRole("button", { name: "完成" }),
    );
    expect(screen.getByText("draft.txt")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "移除待上传文件：draft.txt",
      }),
    );
    await waitFor(() => {
      expect(screen.queryByText("draft.txt")).not.toBeInTheDocument();
    });
    submitCalls = fetchMock.mock.calls.filter(
      ([input, init]) =>
        requestUrl(input).pathname ===
          `/api/v1/sessions/${SESSION.id}/runs` &&
        init?.method === "POST",
    ).length;
    expect(submitCalls).toBe(0);
  });

  it("会话页直接提示不合规文件的校验错误", async () => {
    installFetch({
      runs: () => [],
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await user.upload(
      await screen.findByLabelText("选择会话文件"),
      new File([""], "empty.txt", { type: "text/plain" }),
    );

    expect(
      await screen.findByRole("alert"),
    ).toHaveTextContent("是空文件");
    expect(screen.queryByText("待上传 1 个文件")).not.toBeInTheDocument();
  });

  it("补充回答只携带新文件 ID，不重新绑定技能", async () => {
    const waiting = makeRun({
      id: "run-waiting-input",
      pending_input: {
        created_at: "2026-11-12T03:00:00Z",
        id: "input-1",
        question_message_id: "question-1",
        status: "pending",
      },
      session_id: SESSION.id,
      status: "waiting_for_input",
    });
    let answerBody: Record<string, unknown> | null = null;
    let runs: Run[] = [waiting];
    installFetch({
      files: () => [
        makeSessionFile({
          filename: "sales.csv",
          id: "file-existing",
          session_id: SESSION.id,
        }),
      ],
      messages: () => [
        makeMessage({
          id: "question-1",
          parts: [
            {
              text: "请补充要分析的销售文件。",
              type: "text",
            },
          ],
          sequence: 1,
          session_id: SESSION.id,
        }),
      ],
      onAnswer: (body) => {
        answerBody = body;
        runs = [
          {
            ...waiting,
            pending_input: null,
            status: "queued",
          },
        ];
        return jsonResponse(runs[0]);
      },
      runs: () => runs,
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await user.click(await screen.findByRole("button", { name: "添加附件" }));
    await user.click(
      await screen.findByRole("checkbox", { name: /sales.csv/ }),
    );
    await user.keyboard("{Escape}");
    await user.type(
      screen.getByLabelText("补充信息"),
      "使用这份文件",
    );
    await user.click(
      screen.getByRole("button", { name: "提交回答" }),
    );

    await waitFor(() => expect(answerBody).not.toBeNull());
    expect(answerBody).toMatchObject({
      input_file_ids: ["file-existing"],
      text: "使用这份文件",
    });
    expect(answerBody).not.toHaveProperty("skill_ids");
    expect(
      screen.getByRole("button", { name: /本次技能/ }),
    ).toHaveTextContent("自动选择");
  });

  it("刷新后从服务端恢复多文件列表，并可把已发布产物引用到后续 Run", async () => {
    const completed = makeRun({
      id: "run-artifact-source",
      session_id: SESSION.id,
      status: "completed",
    });
    let submitBody: Record<string, unknown> | null = null;
    const artifact = makeSessionFile({
      filename: "report.xlsx",
      id: "file-refresh-artifact",
      label: "月度销售报表",
      session_id: SESSION.id,
      source: "artifact",
    });
    installFetch({
      files: () => [artifact],
      onSubmit: (body) => {
        submitBody = body;
        return jsonResponse(
          makeRun({
            id: "run-follow-up",
            session_id: SESSION.id,
            status: "queued",
          }),
          201,
        );
      },
      runs: () => [completed],
    });

    const firstUser = userEvent.setup();
    const firstView = renderApp(`/chat/${SESSION.id}`);
    await firstUser.click(
      await screen.findByRole("button", { name: "添加附件" }),
    );
    expect(
      await screen.findByRole("checkbox", { name: /月度销售报表/ }),
    ).toBeInTheDocument();
    firstView.unmount();

    // 模拟浏览器刷新：新的 QueryClient 只能从服务端文件列表恢复引用能力。
    installFetch({
      files: () => [artifact],
      onSubmit: (body) => {
        submitBody = body;
        return jsonResponse(
          makeRun({
            id: "run-follow-up",
            session_id: SESSION.id,
            status: "queued",
          }),
          201,
        );
      },
      runs: () => [completed],
    });
    const user = userEvent.setup();
    renderApp(`/chat/${SESSION.id}`);

    await user.click(await screen.findByRole("button", { name: "添加附件" }));
    await user.click(
      await screen.findByRole("checkbox", { name: /月度销售报表/ }),
    );
    await user.keyboard("{Escape}");
    await user.type(
      screen.getByLabelText("对话草稿"),
      "基于上次产物继续分析",
    );
    await user.click(
      screen.getByRole("button", { name: "发送消息" }),
    );

    await waitFor(() => expect(submitBody).not.toBeNull());
    expect(submitBody).toMatchObject({
      input_file_ids: [artifact.id],
      skill_ids: [],
      text: "基于上次产物继续分析",
    });
  });
});

describe("安全技能审批与多文件交付", () => {
  it("审批和执行详情只展示服务端安全投影，并可下载多文件产物", async () => {
    const pending = {
      approval_summary: null,
      batch_message_id: "batch-skill",
      call_id: "call-skill",
      display_name: null,
      formatted_arguments: [],
      model_name: "skill_execute",
      ordinal: 1,
      requested_at: "2026-11-12T03:00:00Z",
      safe_skill_projection: PROJECTION,
      tool_kind: "builtin",
    };
    const run = makeRun({
      id: "run-skill-approval",
      pending_approvals: [pending],
      session_id: SESSION.id,
      skill_snapshot: {
        selected_skill_ids: [SKILL.id],
        skills: [
          {
            description: SKILL.description,
            name: SKILL.name,
            requires_approval: true,
            scope: "company",
            skill_id: SKILL.id,
            slug: SKILL.slug,
            version_id: "skill-version-1",
            version_label: "v1",
          },
        ],
        version: 1,
      },
      status: "waiting_for_approval",
    });
    const artifacts = [
      makeArtifact({ file_id: "file-report" }),
      makeArtifact({
        file_id: "file-trend",
        filename: "trend.png",
        label: "趋势图",
        mime_type: "image/png",
        size_bytes: 8192,
      }),
    ];
    const call = makeToolCall({
      id: "call-skill",
      model_name: "skill_execute",
      ordinal: 1,
      tool_call_id: "protocol-skill",
      tool_id: "builtin_skill_execute",
    });
    const detail = {
      ...makeToolCallDetail(call),
      arguments: {
        internal_path: "/host/private",
        password: "secret",
      },
      artifacts,
      safe_skill_projection: PROJECTION,
    };
    installFetch({
      files: () => [
        makeSessionFile({
          filename: "sales.csv",
          id: "file-input",
          session_id: SESSION.id,
        }),
        makeSessionFile({
          filename: "report.xlsx",
          id: "file-report",
          label: "月度销售报表",
          session_id: SESSION.id,
          source: "artifact",
        }),
      ],
      messages: () => [
        makeMessage({
          id: "tool-call-message",
          parts: [
            {
              arguments: {
                internal_path: "/host/private",
                password: "secret",
              },
              name: "skill_execute",
              tool_call_id: "protocol-skill",
              type: "tool_call",
            },
          ],
          sequence: 1,
          session_id: SESSION.id,
        }),
        makeMessage({
          id: "tool-result-message",
          parts: [
            {
              artifacts,
              output: "private skill execution log",
              status: "success",
              tool_call_id: "protocol-skill",
              type: "tool_result",
            },
          ],
          sequence: 2,
          session_id: SESSION.id,
        }),
      ],
      runs: () => [run],
      toolCallDetail: () => detail,
      toolCalls: () => [call],
    });
    const user = userEvent.setup();
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:download");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    renderApp(`/chat/${SESSION.id}`);

    const approval = await screen.findByRole("region", {
      name: "等待工具审批",
    });
    expect(within(approval).getByText("销售报表")).toBeInTheDocument();
    expect(within(approval).getByText("v1")).toBeInTheDocument();
    expect(
      within(approval).getByText("根据已授权销售明细生成报表。"),
    ).toBeInTheDocument();
    expect(within(approval).getByText("sales.csv")).toBeInTheDocument();
    expect(
      within(approval).queryByText(/private|secret/),
    ).not.toBeInTheDocument();
    await user.click(
      within(approval).getByRole("button", {
        name: "查看安全详情",
      }),
    );
    expect(
      within(approval).getByText("统计月份"),
    ).toBeInTheDocument();
    expect(
      within(approval).queryByText(/private|secret/),
    ).not.toBeInTheDocument();

    const messageList = screen.getByRole("region", {
      name: "消息历史",
    });
    expect(
      within(messageList).getByText("月度销售报表"),
    ).toBeInTheDocument();
    expect(within(messageList).getByText("趋势图")).toBeInTheDocument();
    expect(
      within(messageList).queryByText(/private skill execution log/),
    ).not.toBeInTheDocument();

    const progress = await screen.findByRole("region", {
      name: "执行进度",
    });
    await user.click(
      within(progress).getByRole("button", { name: /展开/ }),
    );
    const stepList = within(progress).getByRole("list", {
      name: "工具调用步骤",
    });
    await user.click(
      within(stepList).getByRole("button", {
        name: /执行技能脚本/,
      }),
    );
    await within(progress).findByText("交付文件");
    expect(
      within(progress).queryByText(/private|secret/),
    ).not.toBeInTheDocument();

    await user.click(
      within(messageList).getAllByRole("button", {
        name: /下载文件/,
      })[0],
    );
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(createObjectURL).toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    await waitFor(
      () => expect(revokeObjectURL).toHaveBeenCalledWith("blob:download"),
      { timeout: 1_500 },
    );
  });

  it("技能审批缺少安全投影时拒绝读取或展示裸参数", async () => {
    const run = makeRun({
      id: "run-skill-without-projection",
      pending_approvals: [
        {
          approval_summary: null,
          batch_message_id: "batch-missing-projection",
          call_id: "call-missing-projection",
          display_name: null,
          formatted_arguments: [],
          model_name: "skill_execute",
          ordinal: 1,
          requested_at: "2026-11-12T03:00:00Z",
          safe_skill_projection: null,
          tool_kind: "builtin",
        },
      ],
      session_id: SESSION.id,
      status: "waiting_for_approval",
    });
    const fetchMock = installFetch({
      runs: () => [run],
      toolCallDetail: (callId) => ({
        ...makeToolCallDetail({
          id: callId,
          model_name: "skill_execute",
          ordinal: 1,
          tool_call_id: "protocol-missing-projection",
          tool_id: "builtin_skill_execute",
        }),
        arguments: {
          internal_path: "/host/private",
          password: "secret",
        },
      }),
    });

    renderApp(`/chat/${SESSION.id}`);

    const approval = await screen.findByRole("region", {
      name: "等待工具审批",
    });
    expect(
      within(approval).getByText(/没有返回可审批的安全投影/),
    ).toBeInTheDocument();
    expect(
      within(approval).queryByText(/private|secret/),
    ).not.toBeInTheDocument();
    expect(
      within(approval).getByRole("button", {
        name: "安全投影缺失，不可批准",
      }),
    ).toBeDisabled();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        requestUrl(input).pathname.endsWith(
          "/tool-calls/call-missing-projection",
        ),
      ),
    ).toBe(false);
  });

  it("执行进度按三个技能身份隐藏 load/read/execute 的裸参数和结果", async () => {
    const run = makeRun({
      id: "run-skill-trace-projections",
      session_id: SESSION.id,
      status: "completed",
    });
    const calls = [
      makeToolCall({
        id: "call-load",
        model_name: "opaque_loader",
        ordinal: 1,
        tool_call_id: "protocol-load",
        tool_id: "builtin_skill_load",
      }),
      makeToolCall({
        id: "call-read",
        model_name: "skill_read_resource",
        ordinal: 2,
        tool_call_id: "protocol-read",
        tool_id: null,
      }),
      makeToolCall({
        id: "call-execute",
        model_name: "skill_execute",
        ordinal: 3,
        tool_call_id: "protocol-execute",
        tool_id: "builtin_skill_execute",
      }),
    ];
    installFetch({
      runs: () => [run],
      toolCallDetail: (callId) => ({
        ...makeToolCallDetail({
          id: callId,
          model_name: "opaque_tool",
          ordinal: calls.findIndex((call) => call.id === callId) + 1,
          tool_call_id: `protocol-${callId}`,
        }),
        arguments: {
          internal_path: "/host/private",
          password: "secret",
        },
        outcome: {
          cleanup_error_code: null,
          code: "skill_executed",
          data: { internal_log: "private skill output" },
          message: "ok",
          status: "ok",
          truncated: false,
        },
      }),
      toolCalls: () => calls,
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const progress = await screen.findByRole("region", {
      name: "执行进度",
    });
    await user.click(
      within(progress).getByRole("button", { name: /已完成 3 个步骤/ }),
    );
    const stepList = within(progress).getByRole("list", {
      name: "工具调用步骤",
    });
    for (const label of ["加载技能说明", "读取技能资料", "执行技能脚本"]) {
      await user.click(
        await within(stepList).findByRole("button", {
          name: new RegExp(label),
        }),
      );
    }

    expect(
      await within(progress).findAllByText(
        /普通用户详情不会回退展示原始参数、输出或日志/,
      ),
    ).toHaveLength(3);
    expect(
      within(progress).queryByText(/private|secret/),
    ).not.toBeInTheDocument();
  });
});
