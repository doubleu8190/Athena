import { describe, expect, it } from "vitest";

import { ApiError } from "../../shared/api/client";
import { skillErrorMessage } from "./errorMessages";

describe("技能错误映射", () => {
  it("区分个人技能原始请求体过大与正常正文字段上限", () => {
    expect(
      skillErrorMessage(
        new ApiError(413, "skill_personal_request_too_large"),
      ),
    ).toBe(
      "个人技能请求内容过大。正常 100,000 字符正文不应触发；请求体上限为 2 MiB，请刷新后重试。",
    );
  });

  it("数据库不可用时返回稳定的服务提示", () => {
    expect(
      skillErrorMessage(new ApiError(503, "database_unavailable")),
    ).toBe("数据服务暂时不可用，请稍后重试。");
  });
});
