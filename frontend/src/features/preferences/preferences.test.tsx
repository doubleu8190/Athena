import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../shared/api/client";
import { getRequestUrl, makeUser } from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { preferenceErrorMessage } from "./errorMessages";
import {
  applyInstructionPreset,
  CUSTOM_INSTRUCTIONS_MAX_LENGTH,
  USER_MEMORY_CONTENT_MAX_LENGTH,
  validateCustomInstructions,
  validateUserMemoryContent,
  type UserMemory,
  type UserWorkProfile,
} from "./model";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function makeMemory(
  overrides: Partial<UserMemory> & Pick<UserMemory, "id" | "content">,
): UserMemory {
  const { content, id, ...rest } = overrides;
  return {
    content,
    created_at: "2026-11-20T09:00:00Z",
    id,
    source: "manual",
    updated_at: "2026-11-20T09:10:00Z",
    ...rest,
  };
}

interface PreferencesMockOptions {
  createError?: { detail: string; status: number } | null;
  deleteError?: { detail: string; status: number } | null;
  limit?: number;
  memories?: UserMemory[];
  profile?: UserWorkProfile;
  putDelay?: Promise<void> | null;
  putError?: { detail: string; status: number } | null;
  totalOverride?: number;
}

function installPreferencesFetch(options: PreferencesMockOptions = {}) {
  const user = makeUser();
  let profile: UserWorkProfile = options.profile ?? {
    custom_instructions: "原有已保存指令",
    memory_enabled: true,
    updated_at: "2026-11-20T08:00:00Z",
  };
  let memories: UserMemory[] = [...(options.memories ?? [])];
  let limit = options.limit ?? 50;
  let totalOverride = options.totalOverride;
  let putError = options.putError ?? null;
  let putDelay = options.putDelay ?? null;
  let createError = options.createError ?? null;
  let deleteError = options.deleteError ?? null;
  let nextMemorySeq = 1;
  const putPayloads: Array<{
    custom_instructions: string;
    memory_enabled: boolean;
  }> = [];

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      const method = init?.method ?? "GET";

      if (url.pathname.endsWith("/auth/me") && method === "GET") {
        return jsonResponse(user);
      }
      if (url.pathname === "/api/v1/projects" && method === "GET") {
        return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        return jsonResponse({ items: [], page: 1, page_size: 30, total: 0 });
      }
      if (url.pathname === "/api/v1/me/preferences") {
        if (method === "GET") {
          return jsonResponse(profile);
        }
        if (method === "PUT") {
          const body = JSON.parse(String(init?.body)) as {
            custom_instructions: string;
            memory_enabled: boolean;
          };
          putPayloads.push(body);
          if (putDelay) {
            await putDelay;
          }
          if (putError) {
            return jsonResponse({ detail: putError.detail }, putError.status);
          }
          profile = {
            custom_instructions: body.custom_instructions,
            memory_enabled: body.memory_enabled,
            updated_at: "2026-11-24T10:00:00Z",
          };
          return jsonResponse(profile);
        }
      }
      if (url.pathname === "/api/v1/me/memories") {
        if (method === "GET") {
          return jsonResponse({
            items: memories,
            limit,
            total: totalOverride ?? memories.length,
          });
        }
        if (method === "POST") {
          if (createError) {
            return jsonResponse(
              { detail: createError.detail },
              createError.status,
            );
          }
          const body = JSON.parse(String(init?.body)) as { content: string };
          const created = makeMemory({
            content: body.content,
            id: `umem-created-${nextMemorySeq++}`,
            source: "manual",
            updated_at: "2026-11-24T10:05:00Z",
          });
          memories = [created, ...memories];
          totalOverride = undefined;
          return jsonResponse(created, 201);
        }
      }
      const memoryMatch = /^\/api\/v1\/me\/memories\/([^/]+)$/.exec(
        url.pathname,
      );
      if (memoryMatch) {
        const memoryId = decodeURIComponent(memoryMatch[1]);
        if (method === "PATCH") {
          const body = JSON.parse(String(init?.body)) as { content: string };
          const target = memories.find((item) => item.id === memoryId);
          if (!target) {
            return jsonResponse({ detail: "user_memory_not_found" }, 404);
          }
          const updated: UserMemory = {
            ...target,
            content: body.content,
            updated_at: "2026-11-24T10:15:00Z",
          };
          memories = memories.map((item) =>
            item.id === memoryId ? updated : item,
          );
          return jsonResponse(updated);
        }
        if (method === "DELETE") {
          if (deleteError) {
            return jsonResponse(
              { detail: deleteError.detail },
              deleteError.status,
            );
          }
          memories = memories.filter((item) => item.id !== memoryId);
          totalOverride = undefined;
          return new Response(null, { status: 204 });
        }
      }

      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    putPayloads,
    setCreateError(next: { detail: string; status: number } | null) {
      createError = next;
    },
    setDeleteError(next: { detail: string; status: number } | null) {
      deleteError = next;
    },
    setLimit(next: number) {
      limit = next;
    },
    setProfile(next: UserWorkProfile) {
      profile = next;
    },
    setPutDelay(next: Promise<void> | null) {
      putDelay = next;
    },
    setPutError(next: { detail: string; status: number } | null) {
      putError = next;
    },
    setTotalOverride(next: number | undefined) {
      totalOverride = next;
    },
  };
}

describe("个人工作偏好与显式记忆领域规则", () => {
  it("按 Unicode 码点与控制字符校验长期工作指令和显式备忘", () => {
    expect(validateCustomInstructions("")).toBeNull();
    expect(validateCustomInstructions("第一行\n第二行\t缩进")).toBeNull();
    expect(
      validateCustomInstructions("😀".repeat(CUSTOM_INSTRUCTIONS_MAX_LENGTH)),
    ).toBeNull();
    expect(
      validateCustomInstructions(
        "😀".repeat(CUSTOM_INSTRUCTIONS_MAX_LENGTH + 1),
      ),
    ).toContain("4,000");
    expect(validateCustomInstructions("含非法\u0007响铃")).toContain(
      "控制字符",
    );
    expect(validateCustomInstructions("含DEL\u007f字符")).toContain("控制字符");

    expect(validateUserMemoryContent("   ")).toBe("请输入备忘内容。");
    expect(
      validateUserMemoryContent("😀".repeat(USER_MEMORY_CONTENT_MAX_LENGTH)),
    ).toBeNull();
    expect(
      validateUserMemoryContent(
        "😀".repeat(USER_MEMORY_CONTENT_MAX_LENGTH + 1),
      ),
    ).toContain("500");
    expect(validateUserMemoryContent("含空字节\u0000内容")).toContain(
      "控制字符",
    );

    expect(applyInstructionPreset("", "预设 A")).toEqual({
      exceeded: false,
      nextValue: "预设 A",
    });
    expect(applyInstructionPreset("已有指令", "预设 B")).toEqual({
      exceeded: false,
      nextValue: "已有指令\n预设 B",
    });
    const full = "a".repeat(CUSTOM_INSTRUCTIONS_MAX_LENGTH);
    expect(applyInstructionPreset(full, "额外预设")).toEqual({
      exceeded: true,
      nextValue: full,
    });
  });

  it("错误文案查表防御原型属性污染并映射核心错误码", () => {
    expect(
      preferenceErrorMessage(new ApiError(409, "user_memory_limit_reached")),
    ).toContain("上限");
    expect(
      preferenceErrorMessage(new ApiError(404, "user_memory_not_found")),
    ).toContain("不存在");
    expect(
      preferenceErrorMessage(new ApiError(422, "custom_instructions_invalid")),
    ).toContain("4,000");
    for (const key of ["__proto__", "constructor", "toString"]) {
      expect(preferenceErrorMessage(new ApiError(400, key))).toBe(
        "操作未完成，请稍后重试。",
      );
    }
  });
});

describe("工作偏好与显式记忆页面（/preferences）", () => {
  it("支持长期工作指令预设芯片、实时校验、脏草稿防覆盖、清空保存与撤销修改", async () => {
    const controller = installPreferencesFetch({
      profile: {
        custom_instructions: "",
        memory_enabled: true,
        updated_at: null,
      },
    });
    const user = userEvent.setup();
    const app = renderApp("/preferences");

    const textarea = await screen.findByLabelText("工作背景与输出偏好");
    const saveButton = screen.getByRole("button", { name: "保存指令" });
    const undoButton = screen.getByRole("button", { name: "撤销修改" });

    expect(textarea).toHaveValue("");
    expect(saveButton).toBeDisabled();
    expect(undoButton).toBeDisabled();
    expect(textarea).not.toHaveAttribute("aria-invalid");

    // 点击预设芯片填入与换行追加。
    await user.click(
      screen.getByRole("button", { name: /\+ 结论先行与分点表达/ }),
    );
    expect(textarea).toHaveValue(
      "请先给核心结论和建议行动，再补充制度依据或关键数据，尽量使用简洁的中文分点列出。",
    );
    expect(saveButton).toBeEnabled();
    expect(undoButton).toBeEnabled();

    await user.click(
      screen.getByRole("button", { name: /\+ 财务口径与表格呈现/ }),
    );
    expect(String((textarea as HTMLTextAreaElement).value)).toContain(
      "\n涉及金额和经营数据时",
    );

    // 后台服务端基线更新时，绝不覆盖用户正在编辑的脏草稿。
    controller.setProfile({
      custom_instructions: "其他设备写入的新指令",
      memory_enabled: true,
      updated_at: "2026-11-24T09:59:00Z",
    });
    await act(async () => {
      await app.client.invalidateQueries({ queryKey: ["preferences", "profile"] });
    });
    await waitFor(() => {
      expect(String((textarea as HTMLTextAreaElement).value)).toContain(
        "请先给核心结论和建议行动",
      );
    });

    // 超限校验仅在错误态设 aria-invalid 与 aria-describedby，并禁用保存。
    fireEvent.change(textarea, {
      target: { value: "字".repeat(CUSTOM_INSTRUCTIONS_MAX_LENGTH + 1) },
    });
    expect(textarea).toHaveAttribute("aria-invalid", "true");
    expect(textarea).toHaveAttribute("aria-describedby");
    expect(screen.getByRole("alert")).toHaveTextContent("最多 4,000 个字符");
    expect(saveButton).toBeDisabled();

    // 撤销修改恢复到最新服务端基线。
    await user.click(undoButton);
    expect(textarea).toHaveValue("其他设备写入的新指令");
    expect(textarea).not.toHaveAttribute("aria-invalid");
    expect(saveButton).toBeDisabled();

    // 清空后可保存空字符串，完成清除指令。
    await user.clear(textarea);
    expect(saveButton).toBeEnabled();
    await user.click(saveButton);

    await waitFor(() => {
      expect(controller.putPayloads).toEqual([
        { custom_instructions: "", memory_enabled: true },
      ]);
    });
    await waitFor(() => {
      expect(saveButton).toBeDisabled();
    });
  });

  it("显式记忆总开关支持乐观更新与失败回滚，且切换时绝不夹带未保存的指令草稿", async () => {
    const toggleGate = deferred<void>();
    const controller = installPreferencesFetch({
      memories: [
        makeMemory({
          content: "历史保留备忘",
          id: "umem-1",
          source: "chat_explicit",
        }),
      ],
      profile: {
        custom_instructions: "服务端已保存指令",
        memory_enabled: true,
        updated_at: "2026-11-20T08:00:00Z",
      },
      putDelay: toggleGate.promise,
      putError: { detail: "http_error", status: 500 },
    });
    const user = userEvent.setup();

    renderApp("/preferences");

    const textarea = await screen.findByLabelText("工作背景与输出偏好");
    const memorySwitch = screen.getByRole("switch", { name: "启用显式记忆" });
    expect(memorySwitch).toBeChecked();

    // 用户先修改长期指令但不点击保存，形成未保存脏草稿。
    await user.type(textarea, "（未保存草稿）");
    expect(textarea).toHaveValue("服务端已保存指令（未保存草稿）");

    // 点击关闭显式记忆开关：立即乐观切换为未选中并展示温和提示。
    await user.click(memorySwitch);
    await waitFor(() => {
      expect(memorySwitch).not.toBeChecked();
    });
    expect(
      screen.getByText(/已关闭对话自动记忆与注入，历史备忘仍保存在下方/),
    ).toBeInTheDocument();
    // 历史备忘继续可见。
    expect(screen.getByText("历史保留备忘")).toBeInTheDocument();

    // 请求失败后立即回滚开关状态，同时未保存指令草稿完好保留，且提交载荷绝未夹带草稿。
    await act(async () => {
      toggleGate.resolve();
    });
    await waitFor(() => {
      expect(memorySwitch).toBeChecked();
    });
    expect(controller.putPayloads[0]).toEqual({
      custom_instructions: "服务端已保存指令",
      memory_enabled: false,
    });
    expect(textarea).toHaveValue("服务端已保存指令（未保存草稿）");

    // 恢复成功响应后再次关闭：成功落库且仍只提交已保存指令。
    controller.setPutDelay(null);
    controller.setPutError(null);
    await user.click(memorySwitch);
    await waitFor(() => {
      expect(memorySwitch).not.toBeChecked();
    });
    expect(controller.putPayloads[1]).toEqual({
      custom_instructions: "服务端已保存指令",
      memory_enabled: false,
    });
    expect(textarea).toHaveValue("服务端已保存指令（未保存草稿）");
  });

  it("支持显式备忘新增、来源徽章、原地编辑与 AlertDialog 删除确认", async () => {
    const controller = installPreferencesFetch({
      memories: [
        makeMemory({
          content: "周报金额统一用万元",
          id: "umem-chat",
          source: "chat_explicit",
        }),
      ],
    });
    const user = userEvent.setup();

    renderApp("/preferences");

    expect(await screen.findByText("周报金额统一用万元")).toBeInTheDocument();
    expect(screen.getByText("对话记住")).toBeInTheDocument();
    expect(screen.getByText("已保存 1 / 50 条")).toBeInTheDocument();

    // 手动新增一条备忘。
    const addTextarea = screen.getByLabelText("手动添加显式备忘");
    await user.type(addTextarea, "合同评审优先检查付款周期");
    await user.click(screen.getByRole("button", { name: "添加备忘" }));

    expect(
      await screen.findByText("合同评审优先检查付款周期"),
    ).toBeInTheDocument();
    expect(screen.getByText("手动添加")).toBeInTheDocument();
    expect(addTextarea).toHaveValue("");
    expect(screen.getByText("已保存 2 / 50 条")).toBeInTheDocument();

    // 原地编辑备忘。
    await user.click(
      screen.getByRole("button", { name: /编辑备忘：合同评审优先检查付款周期/ }),
    );
    const editTextarea = screen.getByLabelText("编辑备忘内容");
    await user.clear(editTextarea);
    await user.type(editTextarea, "合同评审优先核对付款周期与违约条款");
    await user.click(screen.getByRole("button", { name: "保存" }));

    expect(
      await screen.findByText("合同评审优先核对付款周期与违约条款"),
    ).toBeInTheDocument();

    // 删除确认：先模拟 404 错误保留弹窗反馈，恢复后成功删除。
    controller.setDeleteError({ detail: "user_memory_not_found", status: 404 });
    await user.click(
      screen.getByRole("button", { name: /删除备忘：周报金额统一用万元/ }),
    );
    const alertDialog = screen.getByRole("alertdialog", {
      name: "删除显式备忘",
    });
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );
    expect(
      await within(alertDialog).findByText("该条备忘不存在或已被删除。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("alertdialog", { name: "删除显式备忘" }),
    ).toBeInTheDocument();

    controller.setDeleteError(null);
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("alertdialog", { name: "删除显式备忘" }),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByText("周报金额统一用万元")).not.toBeInTheDocument();
  });

  it("达到 45/50 展示琥珀预警，达到 50/50 禁用新增，并发 409 上限给出明确提示并刷新", async () => {
    const controller = installPreferencesFetch({
      memories: [makeMemory({ content: "已有备忘", id: "umem-existing" })],
      totalOverride: 45,
    });
    const user = userEvent.setup();

    renderApp("/preferences");

    expect(await screen.findByText("已保存 45 / 50 条")).toBeInTheDocument();
    expect(screen.getByText("接近上限，建议精简")).toBeInTheDocument();

    // 模拟并发写入触发 409 user_memory_limit_reached，随后列表刷新为 50 / 50 并禁用新增。
    controller.setCreateError({
      detail: "user_memory_limit_reached",
      status: 409,
    });
    controller.setTotalOverride(50);

    const addTextarea = screen.getByLabelText("手动添加显式备忘");
    await user.type(addTextarea, "触发并发上限的新备忘");
    await user.click(screen.getByRole("button", { name: "添加备忘" }));

    expect(
      await screen.findByText("显式备忘已达上限，请先删除旧备忘后再添加。"),
    ).toBeInTheDocument();
    expect(await screen.findByText("已保存 50 / 50 条")).toBeInTheDocument();
    expect(screen.getByText("已达上限")).toBeInTheDocument();
    expect(addTextarea).toBeDisabled();
    expect(screen.getByRole("button", { name: "添加备忘" })).toBeDisabled();
  });

  it("支持在 /preferences 与 /profile 之间通过顶部个人设置导航切换", async () => {
    installPreferencesFetch();
    const user = userEvent.setup();
    const app = renderApp("/preferences");

    const settingsNav = await screen.findByRole("navigation", {
      name: "个人设置导航",
    });
    expect(
      within(settingsNav).getByRole("link", { name: "工作偏好与记忆" }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(
      within(settingsNav).getByRole("link", { name: "账号与安全" }),
    );
    expect(
      await screen.findByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/profile");
    expect(
      within(
        screen.getByRole("navigation", { name: "个人设置导航" }),
      ).getByRole("link", { name: "账号与安全" }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(
      within(
        screen.getByRole("navigation", { name: "个人设置导航" }),
      ).getByRole("link", { name: "工作偏好与记忆" }),
    );
    expect(
      await screen.findByLabelText("工作背景与输出偏好"),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/preferences");
  });
});
