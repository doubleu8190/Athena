import { describe, expect, it } from "vitest";

import { fileExtension } from "./file";
import { parsePage } from "./pagination";

describe("shared/lib/pagination & file", () => {
  it("parsePage 仅接受正整数，非法或缺失值回退为 1", () => {
    expect(parsePage(null)).toBe(1);
    expect(parsePage("")).toBe(1);
    expect(parsePage("0")).toBe(1);
    expect(parsePage("-3")).toBe(1);
    expect(parsePage("1.5")).toBe(1);
    expect(parsePage("abc")).toBe(1);
    expect(parsePage("2")).toBe(2);
    expect(parsePage("42")).toBe(42);
  });

  it("fileExtension 提取小写末段扩展名并安全处理无后缀与空白", () => {
    expect(fileExtension("report.PDF")).toBe("pdf");
    expect(fileExtension("archive.tar.gz")).toBe("gz");
    expect(fileExtension("notes.md  ")).toBe("md");
    expect(fileExtension(".doc")).toBe("doc");
    expect(fileExtension("README")).toBe("");
    expect(fileExtension("trailing.")).toBe("");
  });
});
