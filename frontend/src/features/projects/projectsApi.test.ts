import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  PROJECT_A,
} from "../../test/fixtures";
import { jsonResponse } from "../../test/testUtils";
import {
  createProject,
  deleteProject,
  getProject,
  listProjects,
  renameProject,
} from "./api";
import { projectErrorMessage } from "./errorMessages";
import { ApiError } from "../../shared/api/client";

describe("项目 API 契约", () => {
  it("序列化分页参数并使用同源 Cookie 请求", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(listResponse([PROJECT_A], 2, 50, 51)),
    );
    vi.stubGlobal("fetch", fetchMock);

    await listProjects({ page: 2, pageSize: 50 });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/projects?page=2&page_size=50",
      expect.objectContaining({
        credentials: "same-origin",
        method: "GET",
      }),
    );
  });

  it("覆盖详情、创建、改名和 204 删除响应", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(PROJECT_A))
      .mockResolvedValueOnce(jsonResponse(PROJECT_A, 201))
      .mockResolvedValueOnce(
        jsonResponse({ ...PROJECT_A, name: "新名称" }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await getProject("project-a");
    await createProject({ name: "增长战略" });
    await renameProject("project-a", { name: "新名称" });
    await expect(deleteProject("project-a")).resolves.toBeUndefined();

    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/projects/project-a",
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({ name: "增长战略" }),
      method: "POST",
    });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify({ name: "新名称" }),
      method: "PATCH",
    });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: "DELETE" });
  });

  it("把名称、占用冲突、404、网络与 HTTP 错误映射为中文", () => {
    expect(
      projectErrorMessage(new ApiError(422, "project_name_invalid")),
    ).toContain("1–120");
    expect(
      projectErrorMessage(new ApiError(409, "project_has_sessions")),
    ).toContain("先删除全部会话");
    expect(
      projectErrorMessage(new ApiError(404, "project_not_found")),
    ).toContain("不存在");
    expect(
      projectErrorMessage(new ApiError(0, "network_error")),
    ).toContain("无法连接服务器");
    expect(
      projectErrorMessage(new ApiError(502, "http_error")),
    ).toContain("服务器拒绝");
  });
});

