import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../shared/api/client";
import {
  getRequestUrl,
  listResponse,
  makeSession,
} from "../../test/fixtures";
import { jsonResponse } from "../../test/testUtils";
import {
  createSession,
  deleteSession,
  getSession,
  listSessions,
  updateSession,
} from "./api";
import { sessionErrorMessage } from "./errorMessages";

const SESSION = makeSession({ id: "session-1" });

describe("Session API 契约", () => {
  it("序列化项目、未归属和分页查询参数", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(listResponse([SESSION])))
      .mockResolvedValueOnce(jsonResponse(listResponse([SESSION])));
    vi.stubGlobal("fetch", fetchMock);

    await listSessions({
      page: 3,
      pageSize: 5,
      projectId: "project-a",
    });
    await listSessions({ page: 1, pageSize: 1, unbound: true });

    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/sessions?page=3&page_size=5&project_id=project-a",
    );
    expect(getRequestUrl(fetchMock.mock.calls[1][0])).toBe(
      "/api/v1/sessions?page=1&page_size=1&unbound=true",
    );
  });

  it("覆盖详情、创建、改名和 204 删除响应", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(SESSION))
      .mockResolvedValueOnce(jsonResponse(SESSION, 201))
      .mockResolvedValueOnce(
        jsonResponse({ ...SESSION, title: "新标题" }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await getSession("session-1");
    await createSession({ project_id: "project-a", title: null });
    await updateSession("session-1", { title: "新标题" });
    await expect(deleteSession("session-1")).resolves.toBeUndefined();

    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/sessions/session-1",
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({
        project_id: "project-a",
        title: null,
      }),
      method: "POST",
    });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify({ title: "新标题" }),
      method: "PATCH",
    });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: "DELETE" });
  });

  it("映射标题、更新、锁定、活跃 Run、404、网络与 HTTP 错误", () => {
    expect(
      sessionErrorMessage(new ApiError(422, "session_title_invalid")),
    ).toContain("1–120");
    expect(
      sessionErrorMessage(new ApiError(422, "session_update_invalid")),
    ).toContain("可更新");
    expect(
      sessionErrorMessage(new ApiError(409, "session_project_locked")),
    ).toContain("不能再更改");
    expect(
      sessionErrorMessage(new ApiError(409, "session_has_active_run")),
    ).toContain("运行中的任务");
    expect(
      sessionErrorMessage(new ApiError(404, "session_not_found")),
    ).toContain("不存在");
    expect(
      sessionErrorMessage(new ApiError(0, "network_error")),
    ).toContain("无法连接服务器");
    expect(
      sessionErrorMessage(new ApiError(500, "http_error")),
    ).toContain("服务器拒绝");
  });
});

