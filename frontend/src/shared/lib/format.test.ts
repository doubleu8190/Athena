import { describe, expect, it } from "vitest";

import { formatBytes, formatDateTime, formatShortDateTime } from "./format";

describe("format utilities", () => {
  it("按 B / KB / MB 统一格式化文件字节数", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytes(2.5 * 1024 * 1024)).toBe("2.5 MB");
  });

  it("非法时间回退为时间未知", () => {
    expect(formatDateTime("not-a-date")).toBe("时间未知");
    expect(formatShortDateTime("not-a-date")).toBe("时间未知");
  });
});
