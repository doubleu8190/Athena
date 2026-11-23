const DATE_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  day: "2-digit",
  hour: "2-digit",
  hour12: false,
  minute: "2-digit",
  month: "2-digit",
  year: "numeric",
});

const SHORT_DATE_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  day: "2-digit",
  hour: "2-digit",
  hour12: false,
  minute: "2-digit",
  month: "2-digit",
});

function parseDate(value: string): Date | null {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(value: string): string {
  const date = parseDate(value);
  return date ? DATE_TIME_FORMATTER.format(date) : "时间未知";
}

export function formatShortDateTime(value: string): string {
  const date = parseDate(value);
  return date ? SHORT_DATE_TIME_FORMATTER.format(date) : "时间未知";
}

// 统一文件体积格式化口径，与既有 B / KB / MB 文案和断言保持一致。
export function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

