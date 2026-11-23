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
import { groupSessionsByTime } from "./model";
import { sessionKeys } from "./queryKeys";

const SESSION = makeSession({ id: "session-1" });

describe("Session API 契约", () => {
  it("序列化项目、未归属、搜索词 q 和分页查询参数", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(listResponse([SESSION])))
      .mockResolvedValueOnce(jsonResponse(listResponse([SESSION])))
      .mockResolvedValueOnce(jsonResponse(listResponse([SESSION])))
      .mockResolvedValueOnce(jsonResponse(listResponse([SESSION])));
    vi.stubGlobal("fetch", fetchMock);

    await listSessions({
      page: 3,
      pageSize: 5,
      projectId: "project-a",
    });
    await listSessions({ page: 1, pageSize: 1, unbound: true });
    await listSessions({ page: 1, pageSize: 30, q: "  季度复盘  " });
    await listSessions({ page: 1, pageSize: 30, q: "   " });

    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/sessions?page=3&page_size=5&project_id=project-a",
    );
    expect(getRequestUrl(fetchMock.mock.calls[1][0])).toBe(
      "/api/v1/sessions?page=1&page_size=1&unbound=true",
    );
    expect(getRequestUrl(fetchMock.mock.calls[2][0])).toBe(
      "/api/v1/sessions?page=1&page_size=30&q=%E5%AD%A3%E5%BA%A6%E5%A4%8D%E7%9B%98",
    );
    expect(getRequestUrl(fetchMock.mock.calls[3][0])).toBe(
      "/api/v1/sessions?page=1&page_size=30",
    );

    // 验证搜索词归一化后纳入查询缓存键，隔离搜索与默认列表缓存。
    expect(sessionKeys.list({ page: 1, pageSize: 30, q: " 复盘 " })).not.toEqual(
      sessionKeys.list({ page: 1, pageSize: 30 }),
    );
    expect(sessionKeys.list({ page: 1, pageSize: 30, q: "   " })).toEqual(
      sessionKeys.list({ page: 1, pageSize: 30 }),
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
    ).toBe("当前对话有正在执行的任务，请等待完成或停止后再操作。");
    expect(
      sessionErrorMessage(new ApiError(400, "constructor")),
    ).toBe("会话操作未完成，请稍后重试。");
    expect(
      sessionErrorMessage(new ApiError(400, "toString")),
    ).toBe("会话操作未完成，请稍后重试。");
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

  it("按本地日历日期将会话分为今天 / 昨天 / 近 7 天 / 更早并省略空组", () => {
    const now = new Date(2026, 10, 23, 15, 30, 0);
    const sessions = [
      makeSession({
        id: "s-today",
        title: "今天会议",
        updated_at: new Date(2026, 10, 23, 9, 0, 0).toISOString(),
      }),
      makeSession({
        id: "s-yesterday",
        title: "昨天复盘",
        updated_at: new Date(2026, 10, 22, 23, 59, 0).toISOString(),
      }),
      makeSession({
        id: "s-week",
        title: "五天前调研",
        updated_at: new Date(2026, 10, 17, 0, 1, 0).toISOString(),
      }),
      makeSession({
        id: "s-earlier",
        title: "更早归档",
        updated_at: new Date(2026, 10, 16, 23, 59, 0).toISOString(),
      }),
      makeSession({
        id: "s-invalid",
        title: "时间异常记录",
        updated_at: "not-a-date",
      }),
    ];

    const groups = groupSessionsByTime(sessions, now);
    expect(groups.map((group) => [group.key, group.label, group.items.map((item) => item.id)])).toEqual([
      ["today", "今天", ["s-today"]],
      ["yesterday", "昨天", ["s-yesterday"]],
      ["last7Days", "近 7 天", ["s-week"]],
      ["earlier", "更早", ["s-earlier", "s-invalid"]],
    ]);

    expect(groupSessionsByTime([sessions[0]], now).map((group) => group.label)).toEqual([
      "今天",
    ]);
    expect(groupSessionsByTime([], now)).toEqual([]);
  });
});

