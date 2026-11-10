import { describe, expect, it } from "vitest";

import { validateDisplayName, validatePassword } from "./validation";

describe("认证字段码点边界", () => {
  it.each([
    ["12 个 emoji", 12, undefined],
    ["128 个 emoji", 128, undefined],
    ["11 个 emoji", 11, "密码需为 12–128 个字符。"],
    ["129 个 emoji", 129, "密码需为 12–128 个字符。"],
  ])("密码支持%s", (_label, count, expected) => {
    expect(validatePassword("😀".repeat(count as number))).toBe(expected);
  });

  it("显示名接受 80 个 emoji 并拒绝 81 个", () => {
    expect(validateDisplayName("😀".repeat(80))).toBeUndefined();
    expect(validateDisplayName("😀".repeat(81))).toBe(
      "显示名最多 80 个字符，且不能包含换行等控制字符。",
    );
  });
});
