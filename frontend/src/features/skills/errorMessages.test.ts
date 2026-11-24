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

  it("完整映射 M3.3 双形态、引导语、slug 与附加授权错误码且避免原型污染", () => {
    expect(
      skillErrorMessage(new ApiError(409, "skill_markdown_edit_unsupported")),
    ).toContain("上传新版本 ZIP");
    expect(
      skillErrorMessage(new ApiError(409, "skill_package_upload_unsupported")),
    ).toContain("在线编辑并发布新版本");
    expect(
      skillErrorMessage(new ApiError(422, "skill_featured_invalid")),
    ).toContain("布尔值");
    expect(
      skillErrorMessage(new ApiError(422, "skill_starter_prompt_invalid")),
    ).toContain("500");
    expect(
      skillErrorMessage(new ApiError(422, "skill_version_label_invalid")),
    ).toContain("64");
    expect(
      skillErrorMessage(new ApiError(422, "skill_slug_invalid")),
    ).toContain("技能标识");
    expect(
      skillErrorMessage(new ApiError(403, "visibility_grant_forbidden")),
    ).toContain("包含下级子部门");
    expect(
      skillErrorMessage(
        new ApiError(422, "visibility_grant_department_not_found"),
      ),
    ).toContain("部门");
    expect(
      skillErrorMessage(new ApiError(500, "toString")),
    ).toBe("技能操作未完成，请稍后重试。");
  });
});
