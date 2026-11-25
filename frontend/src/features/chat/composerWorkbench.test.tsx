import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../shared/ui";
import { makeSkill } from "../../test/adminFixtures";
import {
  FakeSseStream,
  createSseStreamResponse,
} from "../../test/fakeSseStream";
import {
  getRequestUrl,
  knowledgeListResponse,
  listResponse,
  makeRun,
  makeSession,
  makeSessionFile,
  makeUser,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { KnowledgeBase } from "../knowledge/model";
import type { UserModelItem, UserModelListResponse } from "../models/model";

const OFFICE_MODEL: UserModelItem = {
  id: "llm-office",
  is_default: true,
  name: "通用对话模型",
  scenario_description: "适合常规文档撰写与日常问答",
  scenario_label: "日常办公",
  supports_vision: false,
};

const VISION_MODEL: UserModelItem = {
  id: "llm-vision",
  is_default: false,
  name: "多模态视觉模型",
  scenario_description: "适合截图识别、图表解读与深度分析",
  scenario_label: "深度思考",
  supports_vision: true,
};

const FALLBACK_NAME_MODEL: UserModelItem = {
  id: "llm-plain",
  is_default: false,
  name: "标准速答模型",
  scenario_description: null,
  scenario_label: null,
  supports_vision: false,
};

const DEFAULT_MODELS_RESPONSE: UserModelListResponse = {
  default_model_id: OFFICE_MODEL.id,
  items: [OFFICE_MODEL, VISION_MODEL, FALLBACK_NAME_MODEL],
};

const COMPANY_KB: KnowledgeBase = {
  created_at: "2026-11-01T08:00:00Z",
  description: "全公司行政与员工手册",
  id: "kb-company",
  name: "集团规章制度库",
  owner_department_id: null,
  updated_at: "2026-11-10T08:00:00Z",
  visibility_scope: "company",
};

const DEPT_KB: KnowledgeBase = {
  created_at: "2026-11-02T08:00:00Z",
  description: "研发规范与架构评审文档",
  id: "kb-dept",
  name: "研发部技术规范库",
  owner_department_id: "dept-rd",
  updated_at: "2026-11-10T08:00:00Z",
  visibility_scope: "department",
};

const PERSONAL_KB: KnowledgeBase = {
  created_at: "2026-11-03T08:00:00Z",
  description: "个人工作笔记",
  id: "kb-personal",
  name: "我的项目备忘",
  owner_department_id: null,
  updated_at: "2026-11-10T08:00:00Z",
  visibility_scope: "personal",
};

const SESSION = makeSession({
  id: "session-m24",
  project_id: null,
  title: "M2.4 工作台会话",
});

interface WorkbenchFetchOptions {
  user?: ReturnType<typeof makeUser>;
  models?: () => Response;
  knowledgeBases?: () => Response;
  skills?: ReturnType<typeof makeSkill>[];
  onSubmitRun?: (body: Record<string, unknown>) => Response;
  onUploadFile?: (
    file: File,
    idempotencyKey: string | null,
  ) => Response | Promise<Response>;
  sessionFiles?: ReturnType<typeof makeSessionFile>[];
}

function installWorkbenchFetch(options: WorkbenchFetchOptions = {}) {
  const user = options.user ?? TEST_USER;
  const skills = options.skills ?? [];
  const submittedRuns: Record<string, unknown>[] = [];
  const uploadRecords: Array<{ filename: string; key: string | null }> = [];

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";

      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(user);
      }
      if (url.pathname === "/api/v1/models" && method === "GET") {
        return options.models
          ? options.models()
          : jsonResponse(DEFAULT_MODELS_RESPONSE);
      }
      if (url.pathname === "/api/v1/knowledge-bases" && method === "GET") {
        return options.knowledgeBases
          ? options.knowledgeBases()
          : jsonResponse(
              knowledgeListResponse([COMPANY_KB, DEPT_KB, PERSONAL_KB], 1, 100),
            );
      }
      if (url.pathname === "/api/v1/skills" && method === "GET") {
        return jsonResponse({ items: skills });
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
        return jsonResponse(SESSION, 201);
      }
      if (url.pathname === `/api/v1/sessions/${SESSION.id}` && method === "GET") {
        return jsonResponse(SESSION);
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
        method === "GET"
      ) {
        return jsonResponse({ items: [], next_before_sequence: null });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/runs` &&
        method === "GET"
      ) {
        return jsonResponse({ items: [], next_cursor: null });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/runs` &&
        method === "POST"
      ) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        submittedRuns.push(body);
        return (
          options.onSubmitRun?.(body) ??
          jsonResponse(
            makeRun({
              id: `run-${submittedRuns.length}`,
              knowledge_base_ids:
                (body.knowledge_base_ids as string[] | null | undefined) ?? null,
              session_id: SESSION.id,
              status: "completed",
            }),
            201,
          )
        );
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.sessionFiles ?? [],
          next_after_file_id: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "POST"
      ) {
        const file = (init?.body as FormData).get("file") as File;
        const key = new Headers(init?.headers).get("Idempotency-Key");
        uploadRecords.push({ filename: file.name, key });
        return options.onUploadFile
          ? options.onUploadFile(file, key)
          : jsonResponse(
              makeSessionFile({
                filename: file.name,
                id: `file-${uploadRecords.length}`,
                mime_type: file.type || "text/plain",
                session_id: SESSION.id,
                size_bytes: file.size,
              }),
              201,
            );
      }
      const downloadMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/files/([^/]+)/download$`,
      ).exec(url.pathname);
      if (downloadMatch && method === "GET") {
        return new Response("file-download-content", {
          headers: {
            "Content-Disposition": 'attachment; filename="summary.xlsx"',
            "Content-Type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
          status: 200,
        });
      }
      if (url.pathname.endsWith("/stream") && method === "GET") {
        return createSseStreamResponse(url.pathname, init);
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, submittedRuns, uploadRecords };
}

afterEach(() => {
  FakeSseStream.instances = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("M2.4 对话首页与一体化吸底输入器", () => {
  it("场景化模型选择器：加载默认模型、支持切换并携带 model_profile_id，图片附件配非视觉模型展示琥珀提示", async () => {
    const { submittedRuns } = installWorkbenchFetch();
    const user = userEvent.setup();

    renderApp("/chat");

    // 默认展示默认模型的 scenario_label
    const modelTrigger = await screen.findByRole("button", {
      name: /选择模型：日常办公/,
    });
    expect(modelTrigger).toHaveTextContent("日常办公");
    expect(
      screen.queryByTestId("model-vision-warning"),
    ).not.toBeInTheDocument();

    // 附加一张 PNG 图片后，因日常办公模型 supports_vision=false，应展示温和琥珀提醒点
    const imageFile = new File(["fake-png"], "chart.png", {
      type: "image/png",
    });
    await user.upload(screen.getByLabelText("选择任务文件"), imageFile);
    expect(screen.getByText("chart.png")).toBeInTheDocument();
    expect(screen.getByTestId("model-vision-warning")).toBeInTheDocument();

    // 展开模型选择器，验证场景描述、默认徽标、识图徽标与回退 name
    await user.click(modelTrigger);
    const modelDialog = screen.getByRole("dialog", { name: "选择对话模型" });
    expect(
      within(modelDialog).getByText("适合常规文档撰写与日常问答"),
    ).toBeInTheDocument();
    expect(within(modelDialog).getByText("默认")).toBeInTheDocument();
    expect(within(modelDialog).getByText("识图")).toBeInTheDocument();
    expect(within(modelDialog).getByText("标准速答模型")).toBeInTheDocument();

    // 切换为支持视觉的「深度思考」模型后，琥珀提醒点自动消失
    await user.click(
      within(modelDialog).getByRole("option", { name: /深度思考/ }),
    );
    expect(
      screen.getByRole("button", { name: /选择模型：深度思考/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("model-vision-warning"),
    ).not.toBeInTheDocument();

    // 发送消息并在 Run 提交体中验证精确携带 model_profile_id
    await user.type(screen.getByLabelText("对话草稿"), "帮我解读这张图表");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0]).toMatchObject({
      knowledge_base_ids: null,
      model_profile_id: VISION_MODEL.id,
      text: "帮我解读这张图表",
    });
  });

  it("模型目录加载失败或为空时优雅降级显示「默认模型」，绝不阻塞发送", async () => {
    const { submittedRuns } = installWorkbenchFetch({
      models: () => jsonResponse({ detail: "service_unavailable" }, 503),
    });
    const user = userEvent.setup();

    renderApp("/chat");

    const modelTrigger = await screen.findByRole("button", {
      name: /选择模型：默认模型/,
    });
    expect(modelTrigger).toHaveTextContent("默认模型");

    await user.click(modelTrigger);
    expect(
      screen.getByText(/模型目录加载失败，发送时将自动使用系统默认模型/),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.type(screen.getByLabelText("对话草稿"), "模型接口异常时仍可发消息");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0]).toMatchObject({
      model_profile_id: null,
      text: "模型接口异常时仍可发消息",
    });
  });

  it("知识库三态选择器：默认 null、显式多选（分组/搜索/20上限/恢复默认）、关闭 [] 均精确写入 Run 请求体", async () => {
    const twentyOneKbs: KnowledgeBase[] = [
      COMPANY_KB,
      DEPT_KB,
      PERSONAL_KB,
      ...Array.from({ length: 18 }, (_, index) => ({
        created_at: "2026-11-04T08:00:00Z",
        description: `扩展知识库 ${index + 1}`,
        id: `kb-extra-${index + 1}`,
        name: `业务制度库 ${index + 1}`,
        owner_department_id: "dept-rd",
        updated_at: "2026-11-10T08:00:00Z",
        visibility_scope: "department" as const,
      })),
    ];
    const { submittedRuns } = installWorkbenchFetch({
      knowledgeBases: () =>
        jsonResponse(knowledgeListResponse(twentyOneKbs, 1, 100)),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    // 1. 默认态 null：展示「企业与部门知识库」
    const kbTrigger = await screen.findByRole("button", {
      name: /知识库范围：企业与部门知识库/,
    });
    expect(kbTrigger).toHaveAttribute("data-knowledge-mode", "default");

    await user.type(screen.getByLabelText("对话草稿"), "默认知识库测试");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0].knowledge_base_ids).toBeNull();

    // 2. 显式多选：验证三层分组、搜索过滤、20 个上限与恢复默认
    await user.click(kbTrigger);
    const kbDialog = screen.getByRole("dialog", { name: "启用知识库检索" });
    expect(
      await within(kbDialog).findByText("集团规章制度库"),
    ).toBeInTheDocument();
    expect(within(kbDialog).getByText("全公司（1）")).toBeInTheDocument();
    expect(within(kbDialog).getByText("部门共享（19）")).toBeInTheDocument();
    expect(within(kbDialog).getByText("个人（1）")).toBeInTheDocument();

    // 搜索过滤
    const searchBox = within(kbDialog).getByLabelText("搜索知识库");
    await user.type(searchBox, "备忘");
    expect(within(kbDialog).getByText("我的项目备忘")).toBeInTheDocument();
    expect(
      within(kbDialog).queryByText("集团规章制度库"),
    ).not.toBeInTheDocument();
    await user.clear(searchBox);

    // 勾选 20 个知识库后，第 21 个必须被禁用并给出上限提示
    const allCheckboxes = within(kbDialog).getAllByRole("checkbox");
    expect(allCheckboxes).toHaveLength(21);
    for (let index = 0; index < 20; index += 1) {
      await user.click(allCheckboxes[index]);
    }
    expect(allCheckboxes[20]).toBeDisabled();
    expect(
      within(kbDialog).getByText(/单轮对话最多指定 20 个知识库/),
    ).toBeInTheDocument();

    // 恢复默认范围切回 null，再精准勾选 2 个知识库提交
    await user.click(
      within(kbDialog).getByRole("button", { name: "恢复默认范围" }),
    );
    expect(allCheckboxes[20]).toBeEnabled();
    await user.click(
      within(kbDialog).getByRole("checkbox", { name: "集团规章制度库" }),
    );
    await user.click(
      within(kbDialog).getByRole("checkbox", { name: "研发部技术规范库" }),
    );
    await user.click(within(kbDialog).getByRole("button", { name: "完成" }));

    const explicitTrigger = screen.getByRole("button", {
      name: /知识库范围：已选 2 个知识库/,
    });
    expect(explicitTrigger).toHaveAttribute("data-knowledge-mode", "explicit");

    await user.type(screen.getByLabelText("对话草稿"), "显式指定两个知识库");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(2));
    expect(submittedRuns[1].knowledge_base_ids).toEqual([
      COMPANY_KB.id,
      DEPT_KB.id,
    ]);

    // 3. 关闭态 []：关闭 Switch 后按钮显示「不使用知识库」，请求体发送 []
    await user.click(explicitTrigger);
    const kbDialog2 = screen.getByRole("dialog", { name: "启用知识库检索" });
    await user.click(
      within(kbDialog2).getByRole("switch", { name: "启用知识库检索" }),
    );
    await user.click(within(kbDialog2).getByRole("button", { name: "完成" }));

    const offTrigger = screen.getByRole("button", {
      name: /知识库范围：不使用知识库/,
    });
    expect(offTrigger).toHaveAttribute("data-knowledge-mode", "off");

    await user.type(screen.getByLabelText("对话草稿"), "本轮关闭知识库检索");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(3));
    expect(submittedRuns[2].knowledge_base_ids).toEqual([]);
  });

  it("知识库目录请求失败时仍可在默认态与关闭态间切换并提供内联重试", async () => {
    let failDirectory = true;
    installWorkbenchFetch({
      knowledgeBases: () =>
        failDirectory
          ? jsonResponse({ detail: "knowledge_search_unavailable" }, 503)
          : jsonResponse(knowledgeListResponse([COMPANY_KB], 1, 100)),
    });
    const user = userEvent.setup();

    renderApp("/chat");

    await user.click(
      await screen.findByRole("button", {
        name: /知识库范围：企业与部门知识库/,
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "启用知识库检索" });
    expect(
      await within(dialog).findByText("知识库目录加载失败"),
    ).toBeInTheDocument();

    // 失败态下仍能关闭检索并重新开启
    const toggle = within(dialog).getByRole("switch", {
      name: "启用知识库检索",
    });
    await user.click(toggle);
    expect(
      screen.getByRole("button", { name: /知识库范围：不使用知识库/ }),
    ).toBeInTheDocument();
    await user.click(toggle);

    // 点击重试恢复列表
    failDirectory = false;
    await user.click(within(dialog).getByRole("button", { name: "重试" }));
    expect(
      await within(dialog).findByText("集团规章制度库"),
    ).toBeInTheDocument();
  });

  it("对话首页按部门优先展示权限化场景卡片并补齐至 6 张，点击技能/模板聚焦输入框且不覆盖已有草稿", async () => {
    const deptSkill1 = makeSkill({
      description: "自动生成研发周报",
      enabled: true,
      id: "skill-dept-1",
      name: "研发周报助手",
      scope: "department",
    });
    const deptSkill2 = makeSkill({
      description: "架构设计评审清单",
      enabled: true,
      id: "skill-dept-2",
      name: "架构评审官",
      scope: "department",
    });
    const companySkill1 = makeSkill({
      description: "公司制度智能问答",
      enabled: true,
      id: "skill-company-1",
      name: "集团制度百科",
      scope: "company",
    });
    const personalSkill = makeSkill({
      description: "仅自己可见的私有笔记",
      enabled: true,
      id: "skill-personal-1",
      name: "私人备忘技能",
      scope: "personal",
    });

    installWorkbenchFetch({
      skills: [companySkill1, personalSkill, deptSkill1, deptSkill2],
      user: TEST_USER,
    });
    const user = userEvent.setup();

    renderApp("/chat");

    // 问候标题与主部门副标题
    expect(
      await screen.findByRole("heading", { name: "你好，林知远" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "研发部 · 已自动连接企业与本部门知识库，随时协助处理工作",
      ),
    ).toBeInTheDocument();

    // 场景区共 6 张卡片：前 2 张部门技能、第 3 张全公司技能、后 3 张办公模板（不含个人技能）
    const scenarioSection = screen.getByRole("region", {
      name: "常用办公场景与快捷应用",
    });
    await within(scenarioSection).findByText("研发周报助手");
    const cards = within(scenarioSection).getAllByRole("button");
    expect(cards).toHaveLength(6);
    expect(cards[0]).toHaveTextContent("研发周报助手");
    expect(cards[0]).toHaveTextContent("部门共享");
    expect(cards[1]).toHaveTextContent("架构评审官");
    expect(cards[2]).toHaveTextContent("集团制度百科");
    expect(cards[2]).toHaveTextContent("全公司");
    expect(
      within(scenarioSection).queryByText("私人备忘技能"),
    ).not.toBeInTheDocument();

    const textarea = screen.getByLabelText("对话草稿");
    // 点击技能卡片：自动勾选技能并聚焦输入框
    await user.click(cards[0]);
    expect(textarea).toHaveFocus();
    expect(
      screen.getByRole("button", { name: "移除技能：研发周报助手" }),
    ).toBeInTheDocument();

    // 点击办公模板卡片：草稿为空时预填引导提示词并聚焦
    await user.click(cards[3]);
    expect(textarea).toHaveValue(
      "请帮我起草一份本周工作周报，包含核心进展、问题风险与下周计划：",
    );
    expect(textarea).toHaveFocus();

    // 用户修改草稿后再点击另一模板卡片：绝不覆盖原草稿，仅平滑聚焦
    await user.clear(textarea);
    await user.type(textarea, "我已手写的专项汇报内容");
    await user.click(cards[4]);
    expect(textarea).toHaveValue("我已手写的专项汇报内容");
    expect(textarea).toHaveFocus();
  });

  it("无所属部门员工在首页展示通用企业共享知识库副标题", async () => {
    installWorkbenchFetch({
      user: makeUser({
        departments: [],
        display_name: "新入职同事",
      }),
    });

    renderApp("/chat");

    expect(
      await screen.findByRole("heading", { name: "你好，新入职同事" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("已自动连接企业共享知识库，随时协助处理工作"),
    ).toBeInTheDocument();
  });

  it("一体化输入器支持拖拽与粘贴即传、Session 失败重试复用 Idempotency-Key，且彻底移除旧夹层 SessionFilesPanel", async () => {
    let failFirstUpload = true;
    const { submittedRuns, uploadRecords } = installWorkbenchFetch({
      onUploadFile: (file) => {
        if (failFirstUpload && file.name === "report.pdf") {
          failFirstUpload = false;
          return jsonResponse({ detail: "file_storage_unavailable" }, 503);
        }
        return jsonResponse(
          makeSessionFile({
            filename: file.name,
            id: `uploaded-${file.name}`,
            mime_type: file.type || "application/pdf",
            session_id: SESSION.id,
            size_bytes: file.size,
          }),
          201,
        );
      },
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const textarea = await screen.findByLabelText("对话草稿");
    // 确认旧版夹层 SessionFilesPanel 已彻底消失
    expect(document.querySelector(".session-files-panel")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "上传并选中" }),
    ).not.toBeInTheDocument();

    // 1. 框内 Drag & Drop 拖入文件 -> 显示虚线遮罩 -> 松开立即触发自动上传（首次 503 失败）
    const composerCard = textarea.closest("[onDragEnter], div.relative");
    expect(composerCard).not.toBeNull();
    const pdfFile = new File(["pdf-bytes"], "report.pdf", {
      type: "application/pdf",
    });
    fireEvent.dragEnter(composerCard as HTMLElement, {
      dataTransfer: { files: [pdfFile], types: ["Files"] },
    });
    expect(screen.getByTestId("composer-drop-overlay")).toHaveTextContent(
      "松开即可附加文件",
    );
    fireEvent.drop(composerCard as HTMLElement, {
      dataTransfer: { files: [pdfFile], types: ["Files"] },
    });
    expect(
      screen.queryByTestId("composer-drop-overlay"),
    ).not.toBeInTheDocument();

    // 失败芯片带内联重试与移除按钮，且发送按钮保持禁用
    const retryButton = await screen.findByRole("button", {
      name: "重试上传：report.pdf",
    });
    expect(uploadRecords).toHaveLength(1);
    const firstKey = uploadRecords[0].key;
    expect(firstKey).toBeTruthy();

    // 点击重试后复用同一 Idempotency-Key，成功后转为就绪芯片并自动选中
    await user.click(retryButton);
    await waitFor(() => expect(uploadRecords).toHaveLength(2));
    expect(uploadRecords[1].key).toBe(firstKey);
    expect(
      await screen.findByRole("button", { name: "移除附件：report.pdf" }),
    ).toBeInTheDocument();

    // 2. 剪贴板粘贴图片截图 -> 立即自动上传并自动附加
    const pastedImage = new File(["png-bytes"], "screenshot.png", {
      type: "image/png",
    });
    fireEvent.paste(textarea, {
      clipboardData: {
        files: [pastedImage],
        getData: () => "",
        items: [],
      },
    });
    expect(
      await screen.findByRole("button", { name: "移除附件：screenshot.png" }),
    ).toBeInTheDocument();

    // 3. 按 Enter 直接发送，同时携带两个已自动上传的文件 ID
    await user.type(textarea, "请分析这两个附件");
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0]).toMatchObject({
      input_file_ids: ["uploaded-report.pdf", "uploaded-screenshot.png"],
      text: "请分析这两个附件",
    });
  });

  it("草稿提交失败移交会话页时完整保留已选模型与知识库范围，切换范围后刷新 request_id 避免幂等冲突", async () => {
    let failFirstSubmit = true;
    const { submittedRuns } = installWorkbenchFetch({
      onSubmitRun: (body) => {
        if (failFirstSubmit) {
          failFirstSubmit = false;
          return jsonResponse({ detail: "network_error" }, 503);
        }
        return jsonResponse(
          makeRun({
            id: "run-recovered",
            knowledge_base_ids:
              (body.knowledge_base_ids as string[] | null | undefined) ?? null,
            session_id: SESSION.id,
            status: "completed",
          }),
          201,
        );
      },
    });
    const user = userEvent.setup();

    renderApp("/chat");

    // 在草稿页切换模型为「深度思考」、关闭知识库检索（[]）
    await user.click(
      await screen.findByRole("button", { name: /选择模型：日常办公/ }),
    );
    await user.click(screen.getByRole("option", { name: /深度思考/ }));

    await user.click(
      screen.getByRole("button", { name: /知识库范围：企业与部门知识库/ }),
    );
    await user.click(
      screen.getByRole("switch", { name: "启用知识库检索" }),
    );
    await user.click(screen.getByRole("button", { name: "完成" }));

    await user.type(screen.getByLabelText("对话草稿"), "草稿失败移交测试");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    // 自动移交到已创建的会话页，且草稿、模型、知识库关闭态全部完整保留
    expect(
      await screen.findByRole("heading", { name: SESSION.title ?? "" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("对话草稿")).toHaveValue("草稿失败移交测试");
    expect(
      screen.getByRole("button", { name: /选择模型：深度思考/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /知识库范围：不使用知识库/ }),
    ).toBeInTheDocument();
    expect(submittedRuns).toHaveLength(1);
    const firstRequestId = submittedRuns[0].request_id;

    // 切换回默认知识库（null）后再次发送，因幂等指纹变更必须使用新 request_id
    await user.click(
      screen.getByRole("button", { name: /知识库范围：不使用知识库/ }),
    );
    await user.click(screen.getByRole("button", { name: "恢复默认范围" }));
    await user.click(screen.getByRole("button", { name: "完成" }));

    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(2));
    expect(submittedRuns[1]).toMatchObject({
      knowledge_base_ids: null,
      model_profile_id: VISION_MODEL.id,
      text: "草稿失败移交测试",
    });
    expect(submittedRuns[1].request_id).not.toBe(firstRequestId);
  });

  it("移除全部图片附件后琥珀视觉提醒消失，且已选模型后台下线时显式告警并可一键恢复默认", async () => {
    let availableModels: UserModelListResponse = DEFAULT_MODELS_RESPONSE;
    const { submittedRuns } = installWorkbenchFetch({
      models: () => jsonResponse(availableModels),
    });
    const user = userEvent.setup();

    const view = renderApp("/chat");

    await screen.findByRole("button", { name: /选择模型：日常办公/ });
    const imageFile = new File(["png"], "screen.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("选择任务文件"), imageFile);
    expect(screen.getByTestId("model-vision-warning")).toBeInTheDocument();

    // 移除全部图片附件后，非视觉模型琥珀提醒必须自动消失
    await user.click(
      screen.getByRole("button", { name: "移除任务文件：screen.png" }),
    );
    expect(
      screen.queryByTestId("model-vision-warning"),
    ).not.toBeInTheDocument();

    // 选择「深度思考」后模拟后台停用该模型并刷新目录 -> 触发器显式告警而非误显默认模型
    await user.click(screen.getByRole("button", { name: /选择模型：日常办公/ }));
    await user.click(screen.getByRole("option", { name: /深度思考/ }));
    expect(
      screen.getByRole("button", { name: /选择模型：深度思考/ }),
    ).toBeInTheDocument();

    availableModels = {
      default_model_id: OFFICE_MODEL.id,
      items: [OFFICE_MODEL],
    };
    await view.client.invalidateQueries({ queryKey: ["models", "catalog"] });

    const unavailableTrigger = await screen.findByRole("button", {
      name: /选择模型：已选模型不可用/,
    });
    await user.click(unavailableTrigger);
    const dialog = screen.getByRole("dialog", { name: "选择对话模型" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "当前已选模型已不可用",
    );

    // 点击「恢复默认模型」重置回 null
    await user.click(
      within(dialog).getByRole("button", { name: "恢复默认模型" }),
    );
    expect(
      screen.getByRole("button", { name: /选择模型：日常办公/ }),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("对话草稿"), "已切回默认模型");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0].model_profile_id).toBeNull();
  });

  it("知识库取消最后一个显式勾选项自动回落 null，且恢复默认范围将焦点转交完成按钮", async () => {
    const { submittedRuns } = installWorkbenchFetch();
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const kbTrigger = await screen.findByRole("button", {
      name: /知识库范围：企业与部门知识库/,
    });
    await user.click(kbTrigger);
    const dialog = screen.getByRole("dialog", { name: "启用知识库检索" });

    const companyCheckbox = await within(dialog).findByRole("checkbox", {
      name: "集团规章制度库",
    });
    await user.click(companyCheckbox);
    expect(
      screen.getByRole("button", { name: /知识库范围：已选 1 个知识库/ }),
    ).toBeInTheDocument();

    // 取消最后一个已选项时应平滑回落默认态 null，而非误关知识库检索
    await user.click(companyCheckbox);
    const revertedTrigger = screen.getByRole("button", {
      name: /知识库范围：企业与部门知识库/,
    });
    expect(revertedTrigger).toHaveAttribute("data-knowledge-mode", "default");

    // 再次勾选后使用「恢复默认范围」，焦点稳定落在「完成」按钮而非回落 body
    await user.click(companyCheckbox);
    const restoreBtn = within(dialog).getByRole("button", {
      name: "恢复默认范围",
    });
    restoreBtn.focus();
    await user.click(restoreBtn);
    expect(within(dialog).getByRole("button", { name: "完成" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "完成" }));

    await user.type(screen.getByLabelText("对话草稿"), "回落默认范围提交");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0].knowledge_base_ids).toBeNull();
  });

  it("草稿上传 503 移交重试复用 Idempotency-Key、4xx 移交重试换新键，且快速双击重试不并发双传", async () => {
    // 场景 A：503 服务端异常中断移交 -> 重试必须复用同一 Idempotency-Key，且双击仅发 1 次请求
    let fail503Once = true;
    let resolveDelayedRetry: ((res: Response) => void) | undefined;
    const scenarioA = installWorkbenchFetch({
      onUploadFile: () => {
        if (fail503Once) {
          fail503Once = false;
          return jsonResponse({ detail: "file_storage_unavailable" }, 503);
        }
        return new Promise<Response>((resolve) => {
          resolveDelayedRetry = resolve;
        });
      },
    });
    const userA = userEvent.setup();
    const viewA = renderApp("/chat");

    await userA.type(await screen.findByLabelText("对话草稿"), "草稿 503 移交");
    await userA.upload(
      screen.getByLabelText("选择任务文件"),
      new File(["first"], "handoff-503.txt", { type: "text/plain" }),
    );
    await userA.click(screen.getByRole("button", { name: "发送消息" }));

    const retry503Btn = await screen.findByRole("button", {
      name: "重试上传：handoff-503.txt",
    });
    expect(scenarioA.uploadRecords).toHaveLength(1);
    const first503Key = scenarioA.uploadRecords[0].key;
    expect(first503Key).toBeTruthy();

    // 同一帧内快速双击重试，验证同步防重入锁只放行一次真实上传请求
    fireEvent.click(retry503Btn);
    fireEvent.click(retry503Btn);
    await waitFor(() => expect(scenarioA.uploadRecords).toHaveLength(2));
    expect(scenarioA.uploadRecords[1].key).toBe(first503Key);

    resolveDelayedRetry?.(
      jsonResponse(
        makeSessionFile({
          filename: "handoff-503.txt",
          id: "file-503-ok",
          session_id: SESSION.id,
        }),
        201,
      ),
    );
    expect(
      await screen.findByRole("button", { name: "移除附件：handoff-503.txt" }),
    ).toBeInTheDocument();
    viewA.unmount();

    // 场景 B：422 客户端明确拒绝中断移交 -> 重试必须换新 Idempotency-Key
    let fail422Once = true;
    const scenarioB = installWorkbenchFetch({
      onUploadFile: (file) => {
        if (fail422Once) {
          fail422Once = false;
          return jsonResponse({ detail: "file_type_unsupported" }, 422);
        }
        return jsonResponse(
          makeSessionFile({
            filename: file.name,
            id: "file-422-ok",
            session_id: SESSION.id,
          }),
          201,
        );
      },
    });
    const userB = userEvent.setup();
    renderApp("/chat");

    await userB.type(await screen.findByLabelText("对话草稿"), "草稿 422 移交");
    await userB.upload(
      screen.getByLabelText("选择任务文件"),
      new File(["second"], "handoff-422.txt", { type: "text/plain" }),
    );
    await userB.click(screen.getByRole("button", { name: "发送消息" }));

    const retry422Btn = await screen.findByRole("button", {
      name: "重试上传：handoff-422.txt",
    });
    expect(scenarioB.uploadRecords).toHaveLength(1);
    const first422Key = scenarioB.uploadRecords[0].key;
    expect(first422Key).toBeTruthy();

    await userB.click(retry422Btn);
    await waitFor(() => expect(scenarioB.uploadRecords).toHaveLength(2));
    expect(scenarioB.uploadRecords[1].key).not.toBe(first422Key);
  });

  it("拖拽遮罩在 Escape、dragend 与进入禁用态时自动复位，且不吞掉纯文本拖放", async () => {
    installWorkbenchFetch({
      onSubmitRun: () =>
        jsonResponse(
          makeRun({
            id: "run-active-lock",
            session_id: SESSION.id,
            status: "running",
          }),
          201,
        ),
    });
    const user = userEvent.setup();
    renderApp(`/chat/${SESSION.id}`);

    const textarea = await screen.findByLabelText("对话草稿");
    const composerCard = textarea.closest("div.relative") as HTMLElement;
    const file = new File(["doc"], "memo.txt", { type: "text/plain" });

    // 1. Escape 兜底复位遮罩
    fireEvent.dragEnter(composerCard, {
      dataTransfer: { files: [file], types: ["Files"] },
    });
    expect(screen.getByTestId("composer-drop-overlay")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(
      screen.queryByTestId("composer-drop-overlay"),
    ).not.toBeInTheDocument();

    // 2. window dragend 兜底复位遮罩
    fireEvent.dragEnter(composerCard, {
      dataTransfer: { files: [file], types: ["Files"] },
    });
    expect(screen.getByTestId("composer-drop-overlay")).toBeInTheDocument();
    fireEvent(window, new Event("dragend"));
    expect(
      screen.queryByTestId("composer-drop-overlay"),
    ).not.toBeInTheDocument();

    // 3. 纯文本拖放不应被 preventDefault 吞掉（fireEvent.drop 返回 true 表示未调用 preventDefault）
    const textDropAllowed = fireEvent.drop(composerCard, {
      dataTransfer: { files: [], types: ["text/plain"] },
    });
    expect(textDropAllowed).toBe(true);

    // 4. 拖拽遮罩或 📎 浮层打开期间，会话进入运行禁用态后均自动复位/关闭
    await user.type(textarea, "触发运行锁定");
    fireEvent.dragEnter(composerCard, {
      dataTransfer: { files: [file], types: ["Files"] },
    });
    expect(screen.getByTestId("composer-drop-overlay")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() =>
      expect(
        screen.queryByTestId("composer-drop-overlay"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "添加附件" })).toBeDisabled();
  });

  it("会话 📎 附件浮层支持勾选引用已有文件并直接下载，无需旧 SessionFilesPanel", async () => {
    const existingArtifact = makeSessionFile({
      filename: "summary.xlsx",
      id: "file-existing-dl",
      label: "季度财务汇总",
      mime_type:
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      session_id: SESSION.id,
      size_bytes: 8192,
      source: "artifact",
    });
    const { submittedRuns } = installWorkbenchFetch({
      sessionFiles: [existingArtifact],
    });
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:session-file");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    const user = userEvent.setup();
    renderApp(`/chat/${SESSION.id}`);

    await user.click(await screen.findByRole("button", { name: "添加附件" }));
    const popover = screen.getByRole("dialog", { name: "引用本会话已有文件" });

    // 1. 直接下载已有会话文件，不误触发勾选状态
    const downloadBtn = await within(popover).findByRole("button", {
      name: "下载文件：季度财务汇总",
    });
    await user.click(downloadBtn);
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(createObjectURL).toHaveBeenCalled();
    expect(
      within(popover).getByRole("checkbox", { name: "季度财务汇总" }),
    ).not.toBeChecked();

    // 2. 勾选引用该文件后发送 Run
    await user.click(
      within(popover).getByRole("checkbox", { name: "季度财务汇总" }),
    );
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("button", { name: "移除附件：季度财务汇总" }),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText("对话草稿"), "继续解读汇总表");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0].input_file_ids).toEqual([existingArtifact.id]);
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it("对话首页优先展示 featured 推荐技能并按部门优先排序，点击挂载并自动填入 starter_prompt 且不覆盖已有草稿", async () => {
    const featuredCompany = makeSkill({
      description: "公司法务合同风险审查",
      enabled: true,
      featured: true,
      format: "markdown",
      id: "skill-featured-company",
      name: "合同审查推荐助手",
      scope: "company",
      starter_prompt: "请粘贴合同正文或上传 PDF，我将逐条标注核心风险。",
    });
    const featuredDept = makeSkill({
      description: "财务报销单据合规审核",
      enabled: true,
      featured: true,
      format: "markdown",
      id: "skill-featured-dept",
      name: "报销审核推荐助手",
      scope: "department",
      starter_prompt: "请上传差旅或费用报销单截图，我将按最新制度核对。",
    });
    const ordinaryDept = makeSkill({
      description: "未推荐的普通部门技能",
      enabled: true,
      featured: false,
      format: "package",
      id: "skill-ordinary-dept",
      name: "普通部门技能",
      scope: "department",
      starter_prompt: "普通技能引导语",
    });

    installWorkbenchFetch({
      skills: [ordinaryDept, featuredCompany, featuredDept],
      user: TEST_USER,
    });
    const user = userEvent.setup();

    renderApp("/chat");

    const scenarioSection = await screen.findByRole("region", {
      name: "常用办公场景与快捷应用",
    });
    await within(scenarioSection).findByText("报销审核推荐助手");

    const cards = within(scenarioSection).getAllByRole("button");
    // 2 张推荐技能（部门优先于公司）+ 4 张通用办公模板补齐共 6 张；非推荐技能不占推荐位
    expect(cards).toHaveLength(6);
    expect(cards[0]).toHaveTextContent("报销审核推荐助手");
    expect(cards[0]).toHaveTextContent("部门共享");
    expect(cards[0]).toHaveTextContent("推荐");
    expect(cards[1]).toHaveTextContent("合同审查推荐助手");
    expect(cards[1]).toHaveTextContent("全公司");
    expect(cards[1]).toHaveTextContent("推荐");
    expect(
      within(scenarioSection).queryByText("普通部门技能"),
    ).not.toBeInTheDocument();

    const textarea = screen.getByLabelText("对话草稿");
    // 1. 输入框为空时点击推荐技能卡：挂载技能 + 自动填入 starter_prompt + 聚焦输入框
    await user.click(cards[0]);
    expect(
      screen.getByRole("button", { name: "移除技能：报销审核推荐助手" }),
    ).toBeInTheDocument();
    expect(textarea).toHaveValue(
      "请上传差旅或费用报销单截图，我将按最新制度核对。",
    );
    expect(textarea).toHaveFocus();

    // 再次点击同一已挂载卡片：不取消挂载，且因当前草稿即该引导语不弹出「未覆盖原草稿」冗余 Toast
    const toastInfoSpy = vi.spyOn(toast, "info");
    await user.click(cards[0]);
    expect(
      screen.getByRole("button", { name: "移除技能：报销审核推荐助手" }),
    ).toBeInTheDocument();
    expect(toastInfoSpy).not.toHaveBeenCalled();

    // 2. 输入框已有用户手写草稿时点击另一推荐技能卡：仅挂载新技能并聚焦，绝不覆盖用户草稿并温和提示
    await user.clear(textarea);
    await user.type(textarea, "这是我手写的专项合同审批请求");
    await user.click(cards[1]);
    expect(
      screen.getByRole("button", { name: "移除技能：合同审查推荐助手" }),
    ).toBeInTheDocument();
    expect(textarea).toHaveValue("这是我手写的专项合同审批请求");
    expect(textarea).toHaveFocus();
    expect(toastInfoSpy).toHaveBeenCalledWith(
      "已挂载技能，当前输入框已有内容，未覆盖原草稿。",
    );
  });
});
