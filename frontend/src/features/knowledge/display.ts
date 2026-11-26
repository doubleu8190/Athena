import type { KnowledgeLocator } from "./model";

const CHANNEL_LABELS: Record<string, string> = {
  keyword: "关键词",
  semantic: "语义",
};

const DEGRADED_REASON_LABELS: Record<string, string> = {
  keyword_tokenizer_unavailable: "关键词分词暂不可用",
  keyword_search_unavailable: "关键词检索暂不可用",
  semantic_search_unavailable: "语义检索暂不可用",
  semantic_profile_incompatible: "语义模型档案不兼容",
  semantic_unavailable: "语义检索暂不可用",
};

const WARNING_LABELS: Record<string, string> = {
  xlsx_formula_cache_missing:
    "XLSX 部分公式缺少缓存值，已按空值处理",
};

function positiveNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : null;
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function lineRange(locator: KnowledgeLocator): string | null {
  const start = positiveNumber(locator.start_line);
  const end = positiveNumber(locator.end_line);
  if (start && end && start !== end) {
    return `第 ${start}–${end} 行`;
  }
  if (start) {
    return `第 ${start} 行`;
  }
  return null;
}

export function formatKnowledgeLocator(
  locator: KnowledgeLocator,
): string {
  const kind = textValue(locator.kind);
  const page = positiveNumber(locator.page);
  const lines = lineRange(locator);
  const heading = textValue(locator.heading);
  const sheet = textValue(locator.sheet);
  const cells = textValue(locator.cell_range);
  const slide = positiveNumber(locator.slide);
  const mediaId = textValue(locator.media_id);

  if (kind === "pdf" && page) {
    return `PDF 第 ${page} 页${mediaId ? ` · ${mediaId}` : ""}`;
  }
  if (kind === "ocr") {
    if (page) {
      return `OCR 第 ${page} 页${mediaId ? ` · ${mediaId}` : ""}`;
    }
    return `OCR 图片${mediaId ? ` · ${mediaId}` : ""}`;
  }
  if (kind === "docx" || kind === "docx_table") {
    return [
      kind === "docx_table" ? "DOCX 表格" : "DOCX",
      lines,
      heading,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  if (kind === "xlsx") {
    return ["XLSX", sheet ? `工作表 ${sheet}` : null, cells ? `行 ${cells}` : null]
      .filter(Boolean)
      .join(" · ");
  }
  if (kind === "pptx" || kind === "pptx_table") {
    return [
      kind === "pptx_table" ? "PPTX 表格" : "PPTX",
      slide ? `第 ${slide} 张幻灯片` : null,
      textValue(locator.shape)
        ? `形状 ${textValue(locator.shape)}`
        : null,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  if (kind === "markdown") {
    return ["Markdown", lines].filter(Boolean).join(" · ");
  }
  if (kind === "text") {
    return ["文本", lines].filter(Boolean).join(" · ");
  }
  if (lines || page || heading || sheet || cells || slide || mediaId) {
    return [
      kind ?? "来源",
      lines,
      page ? `第 ${page} 页` : null,
      heading,
      sheet,
      cells,
      slide ? `第 ${slide} 张` : null,
      mediaId,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  return kind ? `来源类型：${kind}` : "来源定位未知";
}

export function formatKnowledgeLocators(
  locators: KnowledgeLocator[],
): string {
  if (locators.length === 0) {
    return "来源定位未知";
  }
  return locators.map(formatKnowledgeLocator).join("；");
}

export function formatChannels(channels: string[]): string {
  if (channels.length === 0) {
    return "未返回检索通道";
  }
  return channels
    .map((channel) => CHANNEL_LABELS[channel] ?? channel)
    .join(" + ");
}

export function formatDegradedReasons(reasons: string[]): string {
  return reasons
    .map((reason) => DEGRADED_REASON_LABELS[reason] ?? reason)
    .join("；");
}

export function knowledgeWarningLabel(warning: string): string {
  return WARNING_LABELS[warning] ?? warning;
}

export function formatKnowledgeWarnings(warnings: string[]): string {
  return warnings.map(knowledgeWarningLabel).join("；");
}
