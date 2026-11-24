import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  KNOWLEDGE_BASE_A,
  KNOWLEDGE_BASE_B,
  listResponse,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { KnowledgeBase } from "../knowledge/model";

const DEPT_KNOWLEDGE_BASE: KnowledgeBase = {
  ...KNOWLEDGE_BASE_A,
  owner_department_id: "dept-rd",
  visibility_scope: "department",
};

const COMPANY_KNOWLEDGE_BASE: KnowledgeBase = {
  ...KNOWLEDGE_BASE_B,
  owner_department_id: null,
  visibility_scope: "company",
};

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function projectShell(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  associations: KnowledgeBase[],
) {
  const url = requestUrl(input);
  if (url.pathname.endsWith("/auth/me")) {
    return jsonResponse(TEST_USER);
  }
  if (
    url.pathname === "/api/v1/projects" &&
    (!init?.method || init.method === "GET")
  ) {
    return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
  }
  if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
    return jsonResponse(PROJECT_A);
  }
  if (url.pathname === "/api/v1/sessions") {
    return jsonResponse(listResponse([], 1, 10, 0));
  }
  if (url.pathname === "/api/v1/skills") {
    return jsonResponse({ items: [] });
  }
  if (
    url.pathname ===
      `/api/v1/projects/${PROJECT_A.id}/knowledge-bases` &&
    (!init?.method || init.method === "GET")
  ) {
    return jsonResponse(associations);
  }
  return null;
}

describe("项目知识库关联（单页工作文件夹右栏）", () => {
  it("关联弹窗排除已关联项、展示三层可见性标签并在成功后刷新真实关联", async () => {
    let associations = [DEPT_KNOWLEDGE_BASE];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const shell = projectShell(input, init, associations);
        if (shell) {
          return shell;
        }
        const url = requestUrl(input);
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse({
            items: [DEPT_KNOWLEDGE_BASE, COMPANY_KNOWLEDGE_BASE],
            page: 1,
            page_size: 100,
          });
        }
        if (
          url.pathname ===
            `/api/v1/projects/${PROJECT_A.id}/knowledge-bases/${COMPANY_KNOWLEDGE_BASE.id}` &&
          init?.method === "PUT"
        ) {
          associations = [DEPT_KNOWLEDGE_BASE, COMPANY_KNOWLEDGE_BASE];
          return new Response(null, { status: 204 });
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    // 通过旧 /knowledge 路径访问，自动重定向至单页工作文件夹并渲染知识库卡片。
    renderApp(`/projects/${PROJECT_A.id}/knowledge`);

    expect(
      await screen.findByRole("heading", { name: "项目知识库" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("已关联 1 / 20")).toBeInTheDocument();
    expect(
      await screen.findByText(DEPT_KNOWLEDGE_BASE.name),
    ).toBeInTheDocument();
    const configAside = screen.getByRole("complementary", {
      name: "项目上下文配置",
    });
    expect(within(configAside).getByText("研发部")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "关联知识库" }));
    const dialog = screen.getByRole("dialog", { name: "关联知识库" });

    expect(
      await within(dialog).findByText(COMPANY_KNOWLEDGE_BASE.name),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("全公司")).toBeInTheDocument();
    expect(
      within(dialog).queryByText(DEPT_KNOWLEDGE_BASE.name),
    ).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "关联" }));

    expect(
      await within(dialog).findByText("暂无可关联知识库"),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "完成" }));
    expect(await screen.findByText("已关联 2 / 20")).toBeInTheDocument();
    expect(screen.getByText(COMPANY_KNOWLEDGE_BASE.name)).toBeInTheDocument();
  });

  it("关联列表加载中不显示虚构数量且不能打开关联弹窗", async () => {
    const pendingAssociations = new Promise<Response>(() => {});
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (
          url.pathname ===
            `/api/v1/projects/${PROJECT_A.id}/knowledge-bases` &&
          (!init?.method || init.method === "GET")
        ) {
          return pendingAssociations;
        }
        const shell = projectShell(input, init, []);
        if (shell) {
          return shell;
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);

    expect(
      await screen.findByRole("heading", { name: "项目知识库" }),
    ).toBeInTheDocument();
    expect(screen.getByText("已关联数量读取中")).toBeInTheDocument();
    expect(screen.queryByText("已关联 0 / 20")).not.toBeInTheDocument();
    const addButton = screen.getByRole("button", { name: "关联知识库" });
    expect(addButton).toBeDisabled();
    await user.click(addButton);
    expect(
      screen.queryByRole("dialog", { name: "关联知识库" }),
    ).not.toBeInTheDocument();
  });

  it("关联列表失败时显示数量不可用、禁用新增并保留重试", async () => {
    let associationCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (
          url.pathname ===
            `/api/v1/projects/${PROJECT_A.id}/knowledge-bases` &&
          (!init?.method || init.method === "GET")
        ) {
          associationCalls += 1;
          if (associationCalls === 1) {
            throw new TypeError("Failed to fetch");
          }
          return jsonResponse([]);
        }
        const shell = projectShell(input, init, []);
        if (shell) {
          return shell;
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);

    expect(
      await screen.findByRole("heading", { name: "项目知识库" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("已关联数量暂不可用"),
    ).toBeInTheDocument();
    expect(screen.queryByText("已关联 0 / 20")).not.toBeInTheDocument();
    expect(screen.getByText(/无法连接服务器/)).toBeInTheDocument();
    const addButton = screen.getByRole("button", { name: "关联知识库" });
    expect(addButton).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("已关联 0 / 20")).toBeInTheDocument();
    expect(addButton).toBeEnabled();
    expect(associationCalls).toBe(2);
  });

  it("达到 20 个时禁用新增并明确展示上限", async () => {
    const associations = Array.from({ length: 20 }, (_, index) => ({
      ...KNOWLEDGE_BASE_A,
      id: `knowledge-${index}`,
      name: `知识库 ${index + 1}`,
    }));
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const shell = projectShell(input, init, associations);
        if (shell) {
          return shell;
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${requestUrl(input)}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp(`/projects/${PROJECT_A.id}`);

    expect(await screen.findByText("已关联 20 / 20")).toBeInTheDocument();
    const addButton = screen.getByRole("button", { name: "关联知识库" });
    expect(addButton).toBeDisabled();
    expect(
      screen.getByText("已达到项目关联上限，请先解除一个知识库。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET"),
      ),
    ).toBe(false);
  });

  it("后端 409 上限错误会刷新关联状态并显示冲突", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const shell = projectShell(input, init, []);
        if (shell) {
          return shell;
        }
        const url = requestUrl(input);
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse({
            items: [KNOWLEDGE_BASE_A],
            page: 1,
            page_size: 100,
          });
        }
        if (
          url.pathname ===
            `/api/v1/projects/${PROJECT_A.id}/knowledge-bases/${KNOWLEDGE_BASE_A.id}` &&
          init?.method === "PUT"
        ) {
          return jsonResponse(
            { detail: "project_knowledge_limit_reached" },
            409,
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);
    await screen.findByText("还没有关联知识库");
    await user.click(
      screen.getByRole("button", { name: "关联第一个知识库" }),
    );
    const dialog = screen.getByRole("dialog", { name: "关联知识库" });
    await within(dialog).findByText(KNOWLEDGE_BASE_A.name);
    await user.click(within(dialog).getByRole("button", { name: "关联" }));

    expect(
      await within(dialog).findByText(
        "每个项目最多关联 20 个知识库，请先解除一个已有知识库。",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([input, init]) =>
            requestUrl(input).pathname ===
              `/api/v1/projects/${PROJECT_A.id}/knowledge-bases` &&
            (!init?.method || init.method === "GET"),
        ).length,
      ).toBeGreaterThan(1);
    });
  });

  it("通过 Radix AlertDialog 解除关联只删除项目关系并回到空态", async () => {
    let associations = [KNOWLEDGE_BASE_A];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const shell = projectShell(input, init, associations);
        if (shell) {
          return shell;
        }
        const url = requestUrl(input);
        if (
          url.pathname ===
            `/api/v1/projects/${PROJECT_A.id}/knowledge-bases/${KNOWLEDGE_BASE_A.id}` &&
          init?.method === "DELETE"
        ) {
          associations = [];
          return new Response(null, { status: 204 });
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);
    await screen.findByText(KNOWLEDGE_BASE_A.name);
    await user.click(
      screen.getByRole("button", {
        name: `解除知识库关联：${KNOWLEDGE_BASE_A.name}`,
      }),
    );
    const alertDialog = screen.getByRole("alertdialog", {
      name: "解除知识库关联",
    });
    await user.click(
      within(alertDialog).getByRole("button", { name: "解除关联" }),
    );

    expect(
      await screen.findByRole("heading", { name: "还没有关联知识库" }),
    ).toBeInTheDocument();
    expect(screen.getByText("已关联 0 / 20")).toBeInTheDocument();
  });
});
