import { describe, expect, it } from "vitest";

import { countCodePoints } from "./text";

describe("countCodePoints", () => {
  it.each([
    ["空字符串", ""],
    ["纯 ASCII", "Hello, Athena v2!"],
    ["中文 BMP 与标点", "财务审核规范（2026 年修订版）"],
    ["常见 Emoji 代理对", "财务审核😀🎉🚀"],
    ["国旗与肤色 Emoji", "🇨🇳👍🏽👩🏽‍💻"],
    ["ZWJ 复合家庭 Emoji", "👨‍👩‍👧‍👦"],
    ["组合变音符（NFD）", "e\u0301a\u0308o\u0302"],
    ["扩展平面汉字（SIP）", "𠮷野家𠀀𪛖"],
    ["末尾孤立高代理", "abc\ud800"],
    ["开头孤立低代理", "\udc00abc"],
    ["反转代理对（低在前高在后）", "\udc00\ud800"],
    ["孤立高代理后紧跟合法代理对", "\ud800\ud83d\ude00"],
    ["孤立高代理后紧跟普通字符与低代理", "\ud83da\ude00"],
    ["边界代理码元（0xD800/0xDBFF + 0xDC00/0xDFFF）", "\ud800\udc00\udbff\udfff"],
    ["超出代理区临界码元（0xD7FF / 0xE000）", "\ud7ff\udc00\ud800\ue000"],
  ])("与 Array.from(%s).length 完全等价", (_label, sample) => {
    expect(countCodePoints(sample)).toBe(Array.from(sample).length);
  });

  it("大文本（10 万码点混合中英与 Emoji）计数准确", () => {
    const unit = "系统提示词Prompt😀\ud800"; // 11 + 1 + 1 = 13 码点
    const large = unit.repeat(8_000);
    expect(countCodePoints(large)).toBe(Array.from(large).length);
    expect(countCodePoints(large)).toBe(13 * 8_000);
  });
});
