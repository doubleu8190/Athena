import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../shared/api/client";
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
  updateProject,
} from "./api";
import { projectErrorMessage } from "./errorMessages";
import {
  PROJECT_DESCRIPTION_MAX_LENGTH,
  PROJECT_INSTRUCTIONS_MAX_LENGTH,
  validateProjectDescription,
  validateProjectInstructions,
} from "./model";

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

  it("覆盖详情、创建、局部更新（含显式 null 清空）、改名和温和删除（detach_sessions=true）", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(PROJECT_A))
      .mockResolvedValueOnce(
        jsonResponse({ ...PROJECT_A, description: "战略复盘" }, 201),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          ...PROJECT_A,
          default_skill_ids: ["skill-1"],
          description: null,
          instructions: null,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ...PROJECT_A, name: "新名称" }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await getProject("project-a");
    await createProject({ description: "战略复盘", name: "增长战略" });
    await updateProject("project-a", {
      default_skill_ids: ["skill-1"],
      description: null,
      instructions: null,
    });
    await updateProject("project-a", { name: "新名称" });
    await expect(deleteProject("project-a")).resolves.toBeUndefined();
    await expect(
      deleteProject("project-a", { detachSessions: true }),
    ).resolves.toBeUndefined();

    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/projects/project-a",
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({ description: "战略复盘", name: "增长战略" }),
      method: "POST",
    });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify({
        default_skill_ids: ["skill-1"],
        description: null,
        instructions: null,
      }),
      method: "PATCH",
    });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({
      body: JSON.stringify({ name: "新名称" }),
      method: "PATCH",
    });
    expect(getRequestUrl(fetchMock.mock.calls[4][0])).toBe(
      "/api/v1/projects/project-a",
    );
    expect(fetchMock.mock.calls[4][1]).toMatchObject({ method: "DELETE" });
    expect(getRequestUrl(fetchMock.mock.calls[5][0])).toBe(
      "/api/v1/projects/project-a?detach_sessions=true",
    );
    expect(fetchMock.mock.calls[5][1]).toMatchObject({ method: "DELETE" });
  });

  it("按后端口径校验项目简介与专属背景指令，并把全部错误码映射为安全中文", () => {
    expect(validateProjectDescription("")).toBeNull();
    expect(
      validateProjectDescription("简".repeat(PROJECT_DESCRIPTION_MAX_LENGTH)),
    ).toBeNull();
    expect(
      validateProjectDescription(
        "简".repeat(PROJECT_DESCRIPTION_MAX_LENGTH + 1),
      ),
    ).toContain("500");
    expect(validateProjectDescription("含换行\n简介")).toContain("换行");

    expect(validateProjectInstructions("")).toBeNull();
    expect(validateProjectInstructions("第一行\n第二行\t缩进")).toBeNull();
    expect(
      validateProjectInstructions(
        "指".repeat(PROJECT_INSTRUCTIONS_MAX_LENGTH + 1),
      ),
    ).toContain("8,000");
    expect(validateProjectInstructions("含非法\u0007控制")).toContain("控制字符");

    expect(
      projectErrorMessage(new ApiError(422, "project_name_invalid")),
    ).toContain("1–120");
    expect(
      projectErrorMessage(new ApiError(422, "project_description_invalid")),
    ).toContain("500");
    expect(
      projectErrorMessage(new ApiError(422, "project_instructions_invalid")),
    ).toContain("8,000");
    expect(
      projectErrorMessage(
        new ApiError(422, "project_default_skill_ids_invalid"),
      ),
    ).toContain("20");
    expect(
      projectErrorMessage(new ApiError(409, "project_has_sessions")),
    ).toContain("保留历史对话");
    expect(
      projectErrorMessage(new ApiError(409, "session_has_active_run")),
    ).toContain("正在执行的对话任务");
    expect(
      projectErrorMessage(new ApiError(404, "project_not_found")),
    ).toContain("不存在");
    expect(
      projectErrorMessage(new ApiError(0, "network_error")),
    ).toContain("无法连接服务器");
    expect(
      projectErrorMessage(new ApiError(502, "http_error")),
    ).toContain("服务器拒绝");
    for (const protoKey of ["__proto__", "constructor", "toString"]) {
      expect(projectErrorMessage(new ApiError(400, protoKey))).toBe(
        "项目操作未完成，请稍后重试。",
      );
    }
  });
});
