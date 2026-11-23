import { QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  makeSkill,
  makeSkillVersion,
} from "../../test/adminFixtures";
import { getRequestUrl, TEST_USER } from "../../test/fixtures";
import {
  createTestQueryClient,
  jsonResponse,
  renderApp,
} from "../../test/testUtils";
import { SkillCatalogPicker } from "./SkillCatalogPicker";
import type {
  PersonalSkillInput,
  Skill,
  SkillAdminDetails,
  SkillVersion,
} from "./model";
import {
  PERSONAL_SKILL_FILE_MAX_BYTES,
  validatePersonalSkillFile,
  validatePersonalSkillInput,
} from "./model";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function personalSkill(
  overrides: Partial<Skill> = {},
): Skill {
  return makeSkill({
    description: "按照固定结构整理周报。",
    enabled: true,
    id: "personal-skill-1",
    name: "周报整理",
    scope: "personal",
    slug: "weekly-report",
    ...overrides,
  });
}

function personalVersion(
  overrides: Partial<SkillVersion> = {},
): SkillVersion {
  return makeSkillVersion({
    description: "按照固定结构整理周报。",
    files: [
      {
        path: "SKILL.md",
        sha256: "d".repeat(64),
        size_bytes: 96,
        text: true,
      },
    ],
    guide: "# 周报整理\n\n按进展、问题、下周计划输出。",
    id: "personal-version-1",
    name: "周报整理",
    ordinal: 1,
    scripts: [],
    skill_id: "personal-skill-1",
    slug: "weekly-report",
    ...overrides,
  });
}

function personalDetails(
  overrides: Partial<SkillAdminDetails> = {},
  versionOverrides: Partial<SkillVersion> = {},
): SkillAdminDetails {
  const skill = personalSkill(overrides);
  return {
    ...skill,
    versions: [
      personalVersion({
        description: skill.description,
        id: skill.current_version_id ?? "personal-version-1",
        name: skill.name,
        skill_id: skill.id,
        slug: skill.slug,
        ...versionOverrides,
      }),
    ],
  };
}

describe("普通用户个人技能管理", () => {
  it("Markdown 文件校验拒绝错误扩展名、空文件和超过 1 MiB 的文件", () => {
    expect(
      validatePersonalSkillFile(
        new File(["x"], "notes.txt", { type: "text/plain" }),
      ),
    ).toMatch(/只支持 \.md/);
    expect(
      validatePersonalSkillFile(
        new File([], "empty.md", { type: "text/markdown" }),
      ),
    ).toMatch(/不能为空/);
    expect(
      validatePersonalSkillFile(
        new File([new Uint8Array(PERSONAL_SKILL_FILE_MAX_BYTES + 1)], "large.md"),
      ),
    ).toMatch(/1 MiB/);
    expect(
      validatePersonalSkillFile(
        new File(["# guide"], "guide.MD", { type: "text/plain" }),
      ),
    ).toBeNull();
  });

  it("描述允许多行和制表符，技能名称仍保持单行", () => {
    const multiline = validatePersonalSkillInput({
      description: "第一行\n第二行\t补充",
      guide: "# 指南",
      name: "多行描述技能",
    });
    expect(multiline.description).toBeUndefined();

    const singleLineName = validatePersonalSkillInput({
      description: "合法描述",
      guide: "# 指南",
      name: "第一行\n第二行",
    });
    expect(singleLineName.name).toMatch(/控制字符/);
  });

  it("在线创建后立即出现在本人目录并发送幂等创建请求", async () => {
    let created: SkillAdminDetails | null = null;
    let createBody: PersonalSkillInput | null = null;
    let createRequestId: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: created ? [created] : [] });
        }
        if (url.pathname === "/api/v1/me/skills" && method === "POST") {
          createBody = JSON.parse(String(init?.body)) as PersonalSkillInput;
          createRequestId = new Headers(init?.headers).get(
            "Idempotency-Key",
          );
          created = personalDetails({
            current_version_id: "personal-version-1",
            description: createBody.description,
            id: "personal-created",
            name: createBody.name,
            revision: 1,
            slug: "created-personal-skill",
          });
          return jsonResponse(created, 201);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/skills?view=mine");

    await user.click(
      await screen.findByRole("button", { name: "新建第一个个人技能" }),
    );
    const dialog = screen.getByRole("dialog", { name: "新建个人技能" });
    await user.type(within(dialog).getByLabelText("技能名称"), "会议纪要");
    await user.type(
      within(dialog).getByLabelText("技能描述"),
      "提取决策、负责人和截止时间。",
    );
    await user.type(
      within(dialog).getByLabelText("Markdown 正文"),
      "# 会议纪要\n\n只记录明确确认的信息。",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建并启用" }),
    );

    expect(
      await screen.findByText(/已创建并启用「会议纪要」/),
    ).toHaveTextContent("不会自动加入当前对话");
    expect(
      await screen.findByRole("heading", { name: "会议纪要" }),
    ).toBeInTheDocument();
    expect(createBody).toEqual({
      description: "提取决策、负责人和截止时间。",
      guide: "# 会议纪要\n\n只记录明确确认的信息。",
      name: "会议纪要",
    });
    expect(createRequestId).toMatch(/^[A-Za-z0-9._~-]+$/);
  });

  it("只接受大小写不敏感的 .md 文件，解析后进入同一表单且不自动发布", async () => {
    const parsedFiles: File[] = [];
    let createCalls = 0;
    let createBody: PersonalSkillInput | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/me/skills/parse" &&
          method === "POST"
        ) {
          const body = init?.body;
          if (!(body instanceof FormData)) {
            throw new Error("解析请求必须使用 FormData。");
          }
          parsedFiles.push(body.get("file") as File);
          return jsonResponse({
            description: "从 Markdown 元数据读取的说明\n第二行\t补充",
            guide: "# 导入指南\n\n按证据输出。",
            name: "导入技能",
          });
        }
        if (url.pathname === "/api/v1/me/skills" && method === "POST") {
          createCalls += 1;
          createBody = JSON.parse(String(init?.body)) as PersonalSkillInput;
          return jsonResponse(
            personalDetails(
              {
                description: createBody.description,
                name: createBody.name,
              },
              {
                description: createBody.description,
                guide: createBody.guide,
                name: createBody.name,
              },
            ),
            201,
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/skills?view=mine");

    await user.click(
      await screen.findByRole("button", { name: "新建第一个个人技能" }),
    );
    const dialog = screen.getByRole("dialog", { name: "新建个人技能" });
    await user.click(
      within(dialog).getByRole("button", { name: "导入 Markdown" }),
    );
    const input = within(dialog).getByLabelText("选择单个 Markdown 文件");

    fireEvent.change(input, {
      target: {
        files: [
          new File(["not markdown"], "notes.txt", {
            type: "text/plain",
          }),
        ],
      },
    });
    expect(
      await within(dialog).findByText(/只支持 \.md 文件/),
    ).toBeInTheDocument();
    expect(parsedFiles).toHaveLength(0);

    const markdown = new File(
      [
        "---\nname: 导入技能\ndescription: 从 Markdown 元数据读取的说明\n---\n# 导入指南",
      ],
      "workflow.MD",
      { type: "text/plain" },
    );
    fireEvent.change(input, {
      target: { files: [markdown] },
    });

    await waitFor(() => {
      expect(
        within(dialog).getByLabelText("技能名称"),
      ).toHaveValue("导入技能");
    });
    expect(within(dialog).getByLabelText("技能描述")).toHaveValue(
      "从 Markdown 元数据读取的说明\n第二行\t补充",
    );
    expect(within(dialog).getByLabelText("Markdown 正文")).toHaveValue(
      "# 导入指南\n\n按证据输出。",
    );
    expect(
      within(dialog).getByText(/尚未发布/),
    ).toBeInTheDocument();
    expect(parsedFiles).toHaveLength(1);
    expect(parsedFiles[0].name).toBe("workflow.MD");
    expect(createCalls).toBe(0);

    await user.click(
      within(dialog).getByRole("button", { name: "创建并启用" }),
    );
    expect(
      await screen.findByText(/已创建并启用「导入技能」/),
    ).toBeInTheDocument();
    expect(createCalls).toBe(1);
    expect(createBody).toEqual({
      description: "从 Markdown 元数据读取的说明\n第二行\t补充",
      guide: "# 导入指南\n\n按证据输出。",
      name: "导入技能",
    });
  });

  it("解析延迟期间锁定三个字段，完成后只写入服务端解析结果", async () => {
    let resolveParse:
      | ((value: PersonalSkillInput) => void)
      | undefined;
    const parseResult = new Promise<PersonalSkillInput>((resolve) => {
      resolveParse = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/me/skills/parse" &&
          method === "POST"
        ) {
          return jsonResponse(await parseResult);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/skills?view=mine");
    await user.click(
      await screen.findByRole("button", { name: "新建第一个个人技能" }),
    );
    const dialog = screen.getByRole("dialog", { name: "新建个人技能" });
    const name = within(dialog).getByLabelText("技能名称");
    const description = within(dialog).getByLabelText("技能描述");
    const guide = within(dialog).getByLabelText("Markdown 正文");
    await user.type(name, "手写名称");
    await user.type(description, "手写描述");
    await user.type(guide, "# 手写正文");
    await user.click(
      within(dialog).getByRole("button", { name: "导入 Markdown" }),
    );
    fireEvent.change(
      within(dialog).getByLabelText("选择单个 Markdown 文件"),
      {
        target: {
          files: [
            new File(["# parsed"], "delayed.md", {
              type: "text/markdown",
            }),
          ],
        },
      },
    );

    await waitFor(() => expect(name).toBeDisabled());
    expect(description).toBeDisabled();
    expect(guide).toBeDisabled();
    await user.type(name, "不应写入");
    expect(name).toHaveValue("手写名称");

    resolveParse?.({
      description: "解析后的描述",
      guide: "# 解析后的正文",
      name: "解析后的名称",
    });
    await waitFor(() => expect(name).toHaveValue("解析后的名称"));
    expect(description).toHaveValue("解析后的描述");
    expect(guide).toHaveValue("# 解析后的正文");
  });

  it("连续编辑不同技能时清空上一轮选择的文件", async () => {
    const first = personalDetails(
      {
        current_version_id: "version-a",
        id: "skill-a",
        name: "技能 A",
        slug: "skill-a",
      },
      {
        id: "version-a",
        name: "技能 A",
        skill_id: "skill-a",
        slug: "skill-a",
      },
    );
    const second = personalDetails(
      {
        current_version_id: "version-b",
        id: "skill-b",
        name: "技能 B",
        slug: "skill-b",
      },
      {
        id: "version-b",
        name: "技能 B",
        skill_id: "skill-b",
        slug: "skill-b",
      },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [first, second] });
        }
        if (
          url.pathname === "/api/v1/me/skills/skill-a" &&
          method === "GET"
        ) {
          return jsonResponse(first);
        }
        if (
          url.pathname === "/api/v1/me/skills/skill-b" &&
          method === "GET"
        ) {
          return jsonResponse(second);
        }
        if (
          url.pathname === "/api/v1/me/skills/parse" &&
          method === "POST"
        ) {
          return jsonResponse({
            description: "文件描述",
            guide: "# 文件正文",
            name: "文件名称",
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/skills?view=mine");
    await user.click(
      await screen.findByRole("button", {
        name: "编辑个人技能：技能 A",
      }),
    );
    let dialog = await screen.findByRole("dialog", {
      name: "编辑个人技能",
    });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("技能名称")).toHaveValue(
        "技能 A",
      ),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "导入 Markdown" }),
    );
    fireEvent.change(
      within(dialog).getByLabelText("选择单个 Markdown 文件"),
      {
        target: {
          files: [
            new File(["# a"], "old-a.md", {
              type: "text/markdown",
            }),
          ],
        },
      },
    );
    expect(
      await within(dialog).findByText("old-a.md"),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "取消" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "编辑个人技能" }),
      ).not.toBeInTheDocument(),
    );

    await user.click(
      screen.getByRole("button", {
        name: "编辑个人技能：技能 B",
      }),
    );
    dialog = await screen.findByRole("dialog", {
      name: "编辑个人技能",
    });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("技能名称")).toHaveValue(
        "技能 B",
      ),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "导入 Markdown" }),
    );
    expect(
      within(dialog).queryByText("old-a.md"),
    ).not.toBeInTheDocument();
  });

  it("编辑创建新版本、停用和确认删除时使用当前 revision", async () => {
    let details: SkillAdminDetails | null = personalDetails();
    let versionBody: Record<string, unknown> | null = null;
    let versionRequestId: string | null = null;
    let patchBody: Record<string, unknown> | null = null;
    let deleteRevision: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: details ? [details] : [] });
        }
        if (
          url.pathname ===
            "/api/v1/me/skills/personal-skill-1" &&
          method === "GET"
        ) {
          return jsonResponse(details);
        }
        if (
          url.pathname ===
            "/api/v1/me/skills/personal-skill-1/versions" &&
          method === "POST"
        ) {
          versionBody = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          versionRequestId = new Headers(init?.headers).get(
            "Idempotency-Key",
          );
          details = {
            ...(details as SkillAdminDetails),
            current_version_id: "personal-version-2",
            current_version_label: "v2",
            description: String(versionBody.description),
            enabled: true,
            name: String(versionBody.name),
            revision: 2,
            versions: [
              personalVersion({
                description: String(versionBody.description),
                guide: String(versionBody.guide),
                id: "personal-version-2",
                label: "v2",
                name: String(versionBody.name),
                ordinal: 2,
              }),
              ...(details as SkillAdminDetails).versions,
            ],
          };
          return jsonResponse(details, 201);
        }
        if (
          url.pathname ===
            "/api/v1/me/skills/personal-skill-1" &&
          method === "PATCH"
        ) {
          patchBody = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          details = {
            ...(details as SkillAdminDetails),
            enabled: Boolean(patchBody.enabled),
            revision: 3,
          };
          return jsonResponse(details);
        }
        if (
          url.pathname ===
            "/api/v1/me/skills/personal-skill-1" &&
          method === "DELETE"
        ) {
          deleteRevision = url.searchParams.get("expected_revision");
          details = null;
          return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/skills?view=mine");

    const card = await screen.findByRole("listitem");
    await user.click(
      within(card).getByRole("button", {
        name: "编辑个人技能：周报整理",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "编辑个人技能",
    });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("技能名称")).toHaveValue(
        "周报整理",
      ),
    );
    await user.clear(within(dialog).getByLabelText("技能描述"));
    await user.type(
      within(dialog).getByLabelText("技能描述"),
      "按项目拆分进展与风险。",
    );
    await user.clear(within(dialog).getByLabelText("Markdown 正文"));
    await user.type(
      within(dialog).getByLabelText("Markdown 正文"),
      "# 新版周报\n\n先列风险。",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "保存并启用新版本",
      }),
    );

    expect(
      await screen.findByText(/已保存「周报整理」的新版本并启用/),
    ).toBeInTheDocument();
    expect(versionBody).toEqual({
      description: "按项目拆分进展与风险。",
      expected_revision: 1,
      guide: "# 新版周报\n\n先列风险。",
      name: "周报整理",
    });
    expect(versionRequestId).toMatch(/^[A-Za-z0-9._~-]+$/);

    await user.click(
      await screen.findByRole("button", {
        name: "停用个人技能：周报整理",
      }),
    );
    expect(patchBody).toEqual({
      enabled: false,
      expected_revision: 2,
    });
    expect(
      await screen.findByText(
        "已停用「周报整理」，后续对话不会再把该技能列入可用目录。",
      ),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: "删除个人技能：周报整理",
      }),
    );
    const deleteDialog = screen.getByRole("dialog", {
      name: "删除「周报整理」？",
    });
    expect(deleteDialog).toHaveTextContent(
      "进行中的任务和历史记录不受影响",
    );
    await user.click(
      within(deleteDialog).getByRole("button", { name: "确认删除" }),
    );

    expect(deleteRevision).toBe("3");
    expect(
      await screen.findByRole("heading", { name: "还没有个人技能" }),
    ).toBeInTheDocument();
  });

  it("revision 冲突保留草稿，并允许用户显式刷新最新内容", async () => {
    const initial = personalDetails();
    const remote = personalDetails(
      {
        description: "另一页面保存的说明",
        name: "另一页面名称",
        revision: 2,
      },
      {
        description: "另一页面保存的说明",
        guide: "# 另一页面正文",
        name: "另一页面名称",
      },
    );
    let current = initial;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [current] });
        }
        if (
          url.pathname ===
            "/api/v1/me/skills/personal-skill-1" &&
          method === "GET"
        ) {
          return jsonResponse(current);
        }
        if (
          url.pathname ===
            "/api/v1/me/skills/personal-skill-1/versions" &&
          method === "POST"
        ) {
          current = remote;
          return jsonResponse({ detail: "skill_revision_conflict" }, 409);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/skills?view=mine");
    await user.click(
      await screen.findByRole("button", {
        name: "编辑个人技能：周报整理",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "编辑个人技能",
    });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("技能名称")).toHaveValue(
        "周报整理",
      ),
    );
    await user.clear(within(dialog).getByLabelText("技能名称"));
    await user.type(
      within(dialog).getByLabelText("技能名称"),
      "尚未保存的新名称",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "保存并启用新版本",
      }),
    );

    expect(
      await within(dialog).findByText(/当前草稿没有覆盖最新内容/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByLabelText("技能名称"),
    ).toHaveValue("尚未保存的新名称");
    await user.click(
      within(dialog).getByRole("button", { name: "刷新最新内容" }),
    );

    await waitFor(() =>
      expect(within(dialog).getByLabelText("技能名称")).toHaveValue(
        "另一页面名称",
      ),
    );
    expect(
      within(dialog).getByLabelText("Markdown 正文"),
    ).toHaveValue("# 另一页面正文");
  });

  it("聊天选择器显示公共/个人来源，个人技能只显示纯文档", async () => {
    const publicSkill = makeSkill({
      id: "public-skill",
      name: "公共审批技能",
      scope: "public",
      script_requires_approval: true,
      slug: "public-approval",
    });
    const personal = personalSkill({
      id: "personal-chat-skill",
      name: "个人聊天技能",
      script_requires_approval: false,
      slug: "personal-chat",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (
          url.pathname === "/api/v1/skills" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse({ items: [publicSkill, personal] });
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      }),
    );
    const client = createTestQueryClient();
    const user = userEvent.setup();

    render(
      <QueryClientProvider client={client}>
        <SkillCatalogPicker
          onChange={() => undefined}
          selectedSkillIds={[]}
        />
      </QueryClientProvider>,
    );

    await user.click(
      screen.getByRole("button", { name: /本次技能/ }),
    );
    const personalOption = (
      await screen.findByText("个人聊天技能")
    ).closest("label");
    const publicOption = screen.getByText("公共审批技能").closest("label");
    expect(personalOption).not.toBeNull();
    expect(publicOption).not.toBeNull();
    expect(within(personalOption as HTMLElement).getByText("个人")).toBeVisible();
    expect(
      within(personalOption as HTMLElement).getByText("纯文档"),
    ).toBeVisible();
    expect(
      within(personalOption as HTMLElement).queryByText(/脚本执行/),
    ).not.toBeInTheDocument();
    expect(within(publicOption as HTMLElement).getByText("全公司")).toBeVisible();
    expect(
      within(publicOption as HTMLElement).getByText("脚本执行需审批"),
    ).toBeVisible();
  });
});
