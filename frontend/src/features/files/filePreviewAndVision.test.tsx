import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FakeSseStream,
  createSseStreamResponse,
} from "../../test/fakeSseStream";
import {
  getRequestUrl,
  listResponse,
  makeArtifact,
  makeMessage,
  makeRun,
  makeSession,
  makeSessionFile,
  makeToolCall,
  makeToolCallDetail,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp, createTestQueryClient } from "../../test/testUtils";
import { TooltipProvider } from "../../shared/ui";
import { isImageAttachmentFile } from "../chat/ComposerAttachmentBar";
import type { UserModelItem, UserModelListResponse } from "../models/model";
import {
  extractionErrorSummary,
  fileErrorMessage,
} from "./errorMessages";
import {
  CSV_PREVIEW_MAX_COLUMNS,
  CSV_PREVIEW_MAX_ROWS,
  FilePreviewDialog,
  parseCsvPreview,
} from "./FilePreviewDialog";
import { syncSessionFileInCache } from "./hooks";
import {
  canInlinePreviewFile,
  isExtractableOfficeFile,
  isPreviewableImageFile,
  isPreviewableTextFile,
  mergeSafeFileMimeType,
  sessionFilePreviewUrl,
  type FilePreviewTarget,
  type SessionFile,
  type SessionFileContentResponse,
  type SessionFilePage,
} from "./model";
import { sessionFileKeys } from "./queryKeys";
import { ApiError } from "../../shared/api/client";
import type { InfiniteData } from "@tanstack/react-query";

const SESSION = makeSession({
  id: "session-m34",
  project_id: null,
  title: "M3.4 多模态与在线预览会话",
});

const OFFICE_MODEL: UserModelItem = {
  id: "llm-office",
  is_default: true,
  name: "通用对话模型",
  scenario_description: "适合日常办公问答",
  scenario_label: "日常办公",
  supports_vision: false,
};

const VISION_MODEL: UserModelItem = {
  id: "llm-vision",
  is_default: false,
  name: "多模态视觉模型",
  scenario_description: "支持截图与图表识别",
  scenario_label: "深度识图",
  supports_vision: true,
};

const DEFAULT_MODELS: UserModelListResponse = {
  default_model_id: OFFICE_MODEL.id,
  items: [OFFICE_MODEL, VISION_MODEL],
};

function makeContentResponse(
  overrides: Partial<SessionFileContentResponse> & {
    file: SessionFile;
  },
): SessionFileContentResponse {
  const content = overrides.content ?? "";
  return {
    content,
    eof: true,
    next_offset: new TextEncoder().encode(content).byteLength,
    offset: 0,
    source_kind: "raw_text",
    truncated: false,
    ...overrides,
  };
}

interface M34SessionFetchOptions {
  models?: UserModelListResponse;
  messages?: ReturnType<typeof makeMessage>[];
  files?: SessionFile[];
  runs?: ReturnType<typeof makeRun>[];
  toolCalls?: ReturnType<typeof makeToolCall>[];
  toolCallDetail?: (
    callId: string,
  ) => ReturnType<typeof makeToolCallDetail> | undefined;
  onContent?: (
    fileId: string,
    offset: number,
    limitBytes: number | null,
    signal?: AbortSignal | null,
  ) => Response | Promise<Response>;
  onUpload?: (file: File) => Response | Promise<Response>;
  onSubmitRun?: (body: Record<string, unknown>) => Response;
}

function installSessionFetch(options: M34SessionFetchOptions = {}) {
  const submittedRuns: Record<string, unknown>[] = [];
  const contentRequests: Array<{
    fileId: string;
    offset: number;
    limitBytes: number | null;
  }> = [];
  let uploadedCounter = 0;

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";

      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (url.pathname === "/api/v1/models" && method === "GET") {
        return jsonResponse(options.models ?? DEFAULT_MODELS);
      }
      if (url.pathname === "/api/v1/knowledge-bases" && method === "GET") {
        return jsonResponse({ items: [], page: 1, page_size: 100 });
      }
      if (url.pathname === "/api/v1/skills" && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/projects" && method === "GET") {
        return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        return jsonResponse(listResponse([SESSION], 1, 30, 1));
      }
      if (url.pathname === "/api/v1/sessions" && method === "POST") {
        return jsonResponse(SESSION, 201);
      }
      if (url.pathname === `/api/v1/sessions/${SESSION.id}` && method === "GET") {
        return jsonResponse(SESSION);
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.messages ?? [],
          next_before_sequence: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.files ?? [],
          next_after_file_id: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "POST"
      ) {
        const file = (init?.body as FormData).get("file") as File;
        uploadedCounter += 1;
        if (options.onUpload) {
          return options.onUpload(file);
        }
        return jsonResponse(
          makeSessionFile({
            filename: file.name,
            id: `uploaded-${uploadedCounter}`,
            mime_type: file.type || "application/octet-stream",
            session_id: SESSION.id,
            size_bytes: file.size,
          }),
          201,
        );
      }
      const contentMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/files/([^/]+)/content$`,
      ).exec(url.pathname);
      if (contentMatch && method === "GET") {
        const fileId = decodeURIComponent(contentMatch[1]);
        const offset = Number(url.searchParams.get("offset") ?? "0");
        const limitRaw = url.searchParams.get("limit_bytes");
        const limitBytes = limitRaw ? Number(limitRaw) : null;
        contentRequests.push({ fileId, limitBytes, offset });
        if (options.onContent) {
          return options.onContent(fileId, offset, limitBytes, init?.signal);
        }
        const matched = (options.files ?? []).find((item) => item.id === fileId);
        return jsonResponse(
          makeContentResponse({
            content: "默认文本内容",
            file:
              matched ??
              makeSessionFile({
                filename: "note.txt",
                id: fileId,
                mime_type: "text/plain",
                session_id: SESSION.id,
              }),
          }),
        );
      }
      const downloadMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/files/([^/]+)/download$`,
      ).exec(url.pathname);
      if (downloadMatch && method === "GET") {
        return new Response("downloaded-bytes", {
          headers: {
            "Content-Disposition": 'attachment; filename="original.bin"',
            "Content-Type": "application/octet-stream",
          },
          status: 200,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/runs` &&
        method === "GET"
      ) {
        const items = options.runs ?? [];
        return jsonResponse({ items: items.slice(0, 1), next_cursor: null });
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/runs` &&
        method === "POST"
      ) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        submittedRuns.push(body);
        return (
          options.onSubmitRun?.(body) ??
          jsonResponse(
            makeRun({
              id: `run-${submittedRuns.length}`,
              session_id: SESSION.id,
              status: "completed",
            }),
            201,
          )
        );
      }
      const runDetailMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)$`,
      ).exec(url.pathname);
      if (runDetailMatch && method === "GET") {
        const runId = decodeURIComponent(runDetailMatch[1]);
        const matched = (options.runs ?? []).find((item) => item.id === runId);
        return matched
          ? jsonResponse(matched)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      const toolCallsMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/tool-calls$`,
      ).exec(url.pathname);
      if (toolCallsMatch && method === "GET") {
        return jsonResponse({
          items: options.toolCalls ?? [],
          next_after_ordinal: null,
        });
      }
      const toolDetailMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/tool-calls/([^/]+)$`,
      ).exec(url.pathname);
      if (toolDetailMatch && method === "GET") {
        const callId = decodeURIComponent(toolDetailMatch[2]);
        const detail = options.toolCallDetail?.(callId);
        return detail
          ? jsonResponse(detail)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      if (url.pathname.endsWith("/stream") && method === "GET") {
        return createSseStreamResponse(url.pathname, init);
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return { contentRequests, fetchMock, submittedRuns };
}

afterEach(() => {
  FakeSseStream.instances = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("M3.4 文件白名单、错误码与 RFC 4180 CSV 解析安全边界", () => {
  it("严格区分安全位图、纯文本与办公文档，绝不把 SVG / HTML / GIF 当作安全内联图片", () => {
    expect(isPreviewableImageFile("chart.png", "image/png")).toBe(true);
    expect(isPreviewableImageFile("photo.jpg", "image/jpeg")).toBe(true);
    expect(isPreviewableImageFile("banner.webp", "image/webp")).toBe(true);
    expect(isPreviewableImageFile("raw.jpeg", null)).toBe(true);

    // XSS 高危格式、非白名单位图与扩展名/MIME 交叉伪装必须全部拒绝
    expect(isPreviewableImageFile("vector.svg", "image/svg+xml")).toBe(false);
    expect(isPreviewableImageFile("vector.svg", "image/png")).toBe(false);
    expect(isPreviewableImageFile("vector.svg", null)).toBe(false);
    expect(isPreviewableImageFile("evil.png", "image/svg+xml")).toBe(false);
    expect(isPreviewableImageFile("evil.pdf", "image/png")).toBe(false);
    expect(isPreviewableImageFile("fake.png", "application/pdf")).toBe(false);
    expect(isPreviewableImageFile("page.html", "text/html")).toBe(false);
    expect(isPreviewableImageFile("anim.gif", "image/gif")).toBe(false);
    expect(canInlinePreviewFile("vector.svg", "image/svg+xml")).toBe(false);
    expect(canInlinePreviewFile("evil.png", "image/svg+xml")).toBe(false);
    expect(canInlinePreviewFile("evil.pdf", "image/png")).toBe(false);
    expect(canInlinePreviewFile("fake.png", "application/pdf")).toBe(false);
    expect(canInlinePreviewFile("page.html", "text/html")).toBe(false);

    // 输入器图片判定同样严格拒绝高危 MIME 与跨 MIME 伪装
    expect(isImageAttachmentFile("chart.png", "image/png")).toBe(true);
    expect(isImageAttachmentFile("anim.gif", "image/gif")).toBe(true);
    expect(isImageAttachmentFile("fake.png", "application/pdf")).toBe(false);
    expect(isImageAttachmentFile("evil.pdf", "image/png")).toBe(false);
    expect(isImageAttachmentFile("evil.png", "image/svg+xml")).toBe(false);
    expect(
      isImageAttachmentFile("evil.png", "application/xhtml+xml"),
    ).toBe(false);

    expect(isExtractableOfficeFile("report.pdf", "application/pdf")).toBe(true);
    expect(isExtractableOfficeFile("plan.docx", null)).toBe(true);
    expect(isExtractableOfficeFile("sheet.xlsx", null)).toBe(true);
    expect(isExtractableOfficeFile("deck.pptx", null)).toBe(true);
    expect(isExtractableOfficeFile("legacy.doc", "application/msword")).toBe(
      false,
    );
    expect(isExtractableOfficeFile("legacy.doc", "application/pdf")).toBe(
      false,
    );
    expect(isExtractableOfficeFile("evil.pdf", "image/png")).toBe(false);

    expect(isPreviewableTextFile("readme.md", "text/markdown")).toBe(true);
    expect(isPreviewableTextFile("sales.csv", "text/csv")).toBe(true);
    expect(isPreviewableTextFile("config.json", "application/json")).toBe(true);
    expect(isPreviewableTextFile("run.log", "text/plain")).toBe(true);
    expect(isPreviewableTextFile("notes.txt", null)).toBe(true);
    expect(isPreviewableTextFile("index.html", "text/plain")).toBe(false);
    expect(isPreviewableTextFile("fake.txt", "image/png")).toBe(false);

    // 合并双来源 MIME 时，任一方为不安全 MIME 必须立即胜出以触发拒绝
    expect(mergeSafeFileMimeType(null, "image/svg+xml")).toBe("image/svg+xml");
    expect(mergeSafeFileMimeType("image/png", "image/svg+xml")).toBe(
      "image/svg+xml",
    );
    expect(mergeSafeFileMimeType(null, "image/png")).toBe("image/png");

    expect(sessionFilePreviewUrl("sess/1", "file#2")).toBe(
      "/api/v1/sessions/sess%2F1/files/file%232/preview",
    );
  });

  it("syncSessionFileInCache 在元数据无变化时保持原缓存引用，仅确有变更时才更新", () => {
    const client = createTestQueryClient();
    const initialFile = makeSessionFile({
      extracted_text_bytes: 2048,
      extraction_error_code: null,
      extraction_status: "ready",
      filename: "report.pdf",
      id: "file-cache-1",
      mime_type: "application/pdf",
      session_id: SESSION.id,
      size_bytes: 10_240,
    });
    const initialData: InfiniteData<SessionFilePage> = {
      pageParams: [null],
      pages: [{ items: [initialFile], next_after_file_id: null }],
    };
    client.setQueryData(sessionFileKeys.list(SESSION.id), initialData);

    // 全部字段相同时必须返回原引用，避免触发 MessageRow 无意义重渲染
    syncSessionFileInCache(client, SESSION.id, { ...initialFile });
    expect(client.getQueryData(sessionFileKeys.list(SESSION.id))).toBe(
      initialData,
    );

    // 提取状态或字节数变化时返回更新后的缓存对象
    syncSessionFileInCache(client, SESSION.id, {
      ...initialFile,
      extracted_text_bytes: 4096,
    });
    const updated = client.getQueryData<InfiniteData<SessionFilePage>>(
      sessionFileKeys.list(SESSION.id),
    );
    expect(updated).not.toBe(initialData);
    expect(updated?.pages[0].items[0].extracted_text_bytes).toBe(4096);
  });

  it("完整映射 M3.1 错误码并使用 Object.hasOwn 防止原型链污染", () => {
    const codes = [
      "file_preview_unsupported",
      "file_not_text",
      "file_offset_invalid",
      "file_content_not_text",
      "file_range_too_small",
      "document_extract_too_large",
      "document_encrypted",
      "document_macro_unsupported",
      "document_page_limit_exceeded",
      "document_archive_limit_exceeded",
      "document_extracted_text_too_large",
      "document_no_extractable_text",
      "document_type_unsupported",
      "document_extract_timeout",
      "document_parse_failed",
      "file_storage_unavailable",
    ];
    for (const code of codes) {
      const msg = fileErrorMessage(new ApiError(422, code));
      expect(msg).not.toBe("文件操作未完成，请稍后重试。");
    }

    expect(extractionErrorSummary("document_no_extractable_text")).toContain(
      "扫描件或空白文档",
    );
    expect(extractionErrorSummary("document_encrypted")).toContain("加密");
    // 原型属性不得命中查表
    expect(fileErrorMessage(new ApiError(422, "constructor"))).toBe(
      "文件操作未完成，请稍后重试。",
    );
    expect(extractionErrorSummary("toString")).toContain("暂未提取");
  });

  it("RFC 4180 CSV 解析器正确处理双引号转义、字段内逗号/换行及 200×50 上限截断", () => {
    const parsed = parseCsvPreview(
      '\uFEFF部门,说明,备注\r\n"研发部,上海","第一行\n第二行","含 ""引号"" 文本"\r\n财务部,单行,',
    );
    expect(parsed.headers).toEqual(["部门", "说明", "备注"]);
    expect(parsed.rows).toEqual([
      ["研发部,上海", "第一行\n第二行", '含 "引号" 文本'],
      ["财务部", "单行", ""],
    ]);
    expect(parsed.rowsTruncated).toBe(false);
    expect(parsed.columnsTruncated).toBe(false);

    // 构造 205 行数据 × 55 列验证截断保护
    const wideHeader = Array.from({ length: 55 }, (_, i) => `C${i + 1}`).join(
      ",",
    );
    const manyRows = Array.from(
      { length: 205 },
      (_, rowIndex) =>
        Array.from({ length: 55 }, (_, colIndex) => `R${rowIndex}-${colIndex}`).join(
          ",",
        ),
    );
    const truncated = parseCsvPreview([wideHeader, ...manyRows].join("\n"));
    expect(truncated.rows).toHaveLength(CSV_PREVIEW_MAX_ROWS);
    expect(truncated.headers).toHaveLength(CSV_PREVIEW_MAX_COLUMNS);
    expect(truncated.rowsTruncated).toBe(true);
    expect(truncated.columnsTruncated).toBe(true);
  });
});

describe("FilePreviewDialog 5 种内容形态与分页/中止隔离", () => {
  it("PNG 原图灯箱：直出安全 /preview URL、支持缩放切换与加载失败降级", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const imageTarget: FilePreviewTarget = {
      fileId: "img-1",
      filename: "architecture.png",
      mimeType: "image/png",
      sizeBytes: 24_576,
    };

    render(
      <TooltipProvider>
        <FilePreviewDialog
          file={imageTarget}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    const dialog = await screen.findByRole("dialog", {
      name: "architecture.png",
    });
    const img = within(dialog).getByRole("img", { name: "architecture.png" });
    expect(img).toHaveAttribute(
      "src",
      `/api/v1/sessions/${SESSION.id}/files/img-1/preview`,
    );
    // 位图灯箱直出 /preview，不请求文本 /content 接口
    expect(fetchMock).not.toHaveBeenCalled();

    // 切换 100% 原始尺寸 / 适应窗口，并校验按钮在适应窗口与原始尺寸态的布局约束类
    fireEvent.load(img);
    const fitImageButton = within(dialog).getByRole("button", {
      name: "切换至 100% 原始尺寸：architecture.png",
    });
    expect(fitImageButton).toHaveClass("min-w-0", "max-w-full");

    await user.click(
      within(dialog).getByRole("button", { name: "100% 原始尺寸" }),
    );
    expect(
      within(dialog).getByRole("button", { name: "适应窗口" }),
    ).toBeInTheDocument();
    const actualImageButton = within(dialog).getByRole("button", {
      name: "缩放至适应窗口：architecture.png",
    });
    expect(actualImageButton).toHaveClass("shrink-0");

    // 图片加载失败时优雅降级提示，并保留重新加载与下载原件按钮
    fireEvent.error(img);
    expect(
      within(dialog).getByText("原图预览加载失败"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "重新加载" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "下载文件：architecture.png" }),
    ).toBeInTheDocument();
  });

  it("Markdown 文档：默认安全渲染，支持「源码」切换与一键复制", async () => {
    const mdFile = makeSessionFile({
      filename: "summary.md",
      id: "file-md",
      mime_type: "text/markdown",
      session_id: SESSION.id,
      size_bytes: 120,
    });
    const mdText = "# Q4 总结\n\n核心结论：**营收增长 18%**";
    installSessionFetch({
      files: [mdFile],
      onContent: () =>
        jsonResponse(
          makeContentResponse({ content: mdText, file: mdFile }),
        ),
    });
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            fileId: mdFile.id,
            filename: mdFile.filename,
            mimeType: mdFile.mime_type,
            sizeBytes: mdFile.size_bytes,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    const dialog = await screen.findByRole("dialog", { name: "summary.md" });
    expect(
      await within(dialog).findByRole("heading", { name: "Q4 总结" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("营收增长 18%")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "源码" }));
    expect(within(dialog).getByTestId("file-preview-raw-text")).toHaveTextContent(
      "# Q4 总结",
    );

    await user.click(within(dialog).getByRole("button", { name: "一键复制" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(mdText));
  });

  it("CSV 表格：RFC 4180 安全渲染（含引号逗号/换行与脚本转义）并支持切换原始文本", async () => {
    const csvFile = makeSessionFile({
      filename: "sales.csv",
      id: "file-csv",
      mime_type: "text/csv",
      session_id: SESSION.id,
      size_bytes: 256,
    });
    const csvText =
      '区域,备注,金额\n"华东,上海","第一行\n第二行",12800\n"<script>alert(1)</script>","含 ""VIP"" 客户",9500';
    installSessionFetch({
      files: [csvFile],
      onContent: () =>
        jsonResponse(makeContentResponse({ content: csvText, file: csvFile })),
    });
    const user = userEvent.setup();

    render(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            fileId: csvFile.id,
            filename: csvFile.filename,
            mimeType: csvFile.mime_type,
            sizeBytes: csvFile.size_bytes,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    const dialog = await screen.findByRole("dialog", { name: "sales.csv" });
    const table = await within(dialog).findByTestId("csv-preview-table");
    expect(within(table).getByText("华东,上海")).toBeInTheDocument();
    expect(within(table).getByText('含 "VIP" 客户')).toBeInTheDocument();
    // 引号内换行与 HTML 字符串均作为纯文本安全展示，不注入真实 <script> 标签
    expect(table.querySelector("script")).toBeNull();
    expect(
      within(table).getByText("<script>alert(1)</script>"),
    ).toBeInTheDocument();
    expect(table.textContent).toContain("第一行\n第二行");

    await user.click(within(dialog).getByRole("button", { name: "原始文本" }));
    expect(
      within(dialog).getByTestId("file-preview-raw-text"),
    ).toHaveTextContent('"华东,上海"');
  });

  it("CSV 超出 200 行上限时截断表格渲染并展示下载全量提示", async () => {
    const bigCsvFile = makeSessionFile({
      filename: "large-ledger.csv",
      id: "file-big-csv",
      mime_type: "text/csv",
      session_id: SESSION.id,
      size_bytes: 20_480,
    });
    const lines = [
      "序号,金额",
      ...Array.from({ length: 205 }, (_, idx) => `${idx + 1},${(idx + 1) * 10}`),
    ];
    installSessionFetch({
      files: [bigCsvFile],
      onContent: () =>
        jsonResponse(
          makeContentResponse({ content: lines.join("\n"), file: bigCsvFile }),
        ),
    });

    render(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            fileId: bigCsvFile.id,
            filename: bigCsvFile.filename,
            mimeType: bigCsvFile.mime_type,
            sizeBytes: bigCsvFile.size_bytes,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    const dialog = await screen.findByRole("dialog", {
      name: "large-ledger.csv",
    });
    const table = await within(dialog).findByTestId("csv-preview-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(
      CSV_PREVIEW_MAX_ROWS,
    );
    expect(
      within(dialog).getByText(/仅展示前 200 行数据 × 50 列/),
    ).toBeInTheDocument();
  });

  it("JSON 文件完整未截断时自动缩进格式化展示", async () => {
    const jsonFile = makeSessionFile({
      filename: "metrics.json",
      id: "file-json",
      mime_type: "application/json",
      session_id: SESSION.id,
      size_bytes: 64,
    });
    installSessionFetch({
      files: [jsonFile],
      onContent: () =>
        jsonResponse(
          makeContentResponse({
            content: '{"revenue":1200,"ok":true}',
            eof: true,
            file: jsonFile,
          }),
        ),
    });

    render(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            fileId: jsonFile.id,
            filename: jsonFile.filename,
            mimeType: jsonFile.mime_type,
            sizeBytes: jsonFile.size_bytes,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    const dialog = await screen.findByRole("dialog", { name: "metrics.json" });
    const pre = await within(dialog).findByTestId("file-preview-raw-text");
    expect(pre.textContent).toBe('{\n  "revenue": 1200,\n  "ok": true\n}');
    expect(within(dialog).getByText("已格式化")).toBeInTheDocument();
  });

  it("PDF / Office 提取文档支持 next_offset 分页「加载更多」以及 422 提取失败友好降级", async () => {
    const pdfFile = makeSessionFile({
      extracted_text_bytes: 120_000,
      extraction_error_code: null,
      extraction_status: "ready",
      filename: "2026年Q3财务报告.pdf",
      id: "file-pdf-paged",
      mime_type: "application/pdf",
      session_id: SESSION.id,
      size_bytes: 184_320,
    });
    const encryptedDoc = makeSessionFile({
      extracted_text_bytes: null,
      extraction_error_code: null,
      extraction_status: "none",
      filename: "加密合同.pdf",
      id: "file-pdf-encrypted",
      mime_type: "application/pdf",
      session_id: SESSION.id,
      size_bytes: 96_000,
    });

    const onMetadataUpdated = vi.fn();
    const { contentRequests } = installSessionFetch({
      files: [pdfFile, encryptedDoc],
      onContent: (fileId, offset) => {
        if (fileId === encryptedDoc.id) {
          return jsonResponse({ detail: "document_encrypted" }, 422);
        }
        if (offset === 0) {
          return jsonResponse(
            makeContentResponse({
              content: "## 第 1 页\n第一页营收概要",
              eof: false,
              file: pdfFile,
              next_offset: 65_536,
              offset: 0,
              source_kind: "extracted_document",
              truncated: true,
            }),
          );
        }
        return jsonResponse(
          makeContentResponse({
            content: "\n\n## 第 2 页\n第二页成本明细",
            eof: true,
            file: pdfFile,
            next_offset: 120_000,
            offset: 65_536,
            source_kind: "extracted_document",
            truncated: false,
          }),
        );
      },
    });
    const user = userEvent.setup();

    const { rerender } = render(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            extractedTextBytes: pdfFile.extracted_text_bytes,
            extractionStatus: pdfFile.extraction_status,
            fileId: pdfFile.id,
            filename: pdfFile.filename,
            mimeType: pdfFile.mime_type,
            sizeBytes: pdfFile.size_bytes,
          }}
          onFileMetadataUpdated={onMetadataUpdated}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    const dialog = await screen.findByRole("dialog", {
      name: "2026年Q3财务报告.pdf",
    });
    expect(
      await within(dialog).findByRole("heading", { name: "第 1 页" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("第一页营收概要")).toBeInTheDocument();
    expect(onMetadataUpdated).toHaveBeenCalledWith(pdfFile);

    // 点击「加载更多」必须传入后端返回的 next_offset=65536，并拼接第二页内容直至 eof
    await user.click(within(dialog).getByRole("button", { name: "加载更多" }));
    expect(
      await within(dialog).findByRole("heading", { name: "第 2 页" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("第一页营收概要")).toBeInTheDocument();
    expect(within(dialog).getByText("第二页成本明细")).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "加载更多" }),
    ).not.toBeInTheDocument();
    expect(contentRequests.map((req) => req.offset)).toEqual([0, 65_536]);

    // 切换为加密文档：请求 /content 返回 422 document_encrypted 时展示温和中文降级卡片与下载原件入口
    rerender(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            extractionStatus: "none",
            fileId: encryptedDoc.id,
            filename: encryptedDoc.filename,
            mimeType: encryptedDoc.mime_type,
            sizeBytes: encryptedDoc.size_bytes,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    expect(
      await within(dialog).findByText("暂无法在线提取文档文本"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/文档已加密保护/)).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "下载文件：加密合同.pdf" }),
    ).toBeInTheDocument();
  });

  it("切文件或关闭弹窗时立即 Abort 在途请求并重置状态，杜绝旧响应串染", async () => {
    let resolveSlowFirst: ((res: Response) => void) | undefined;
    let firstAborted = false;
    const fileA = makeSessionFile({
      filename: "slow.txt",
      id: "file-slow",
      mime_type: "text/plain",
      session_id: SESSION.id,
    });
    const fileB = makeSessionFile({
      filename: "fast.txt",
      id: "file-fast",
      mime_type: "text/plain",
      session_id: SESSION.id,
    });

    installSessionFetch({
      files: [fileA, fileB],
      onContent: (fileId, _offset, _limit, signal) => {
        if (fileId === fileA.id) {
          signal?.addEventListener("abort", () => {
            firstAborted = true;
          });
          return new Promise<Response>((resolve) => {
            resolveSlowFirst = resolve;
          });
        }
        return jsonResponse(
          makeContentResponse({ content: "新文件 B 的权威内容", file: fileB }),
        );
      },
    });

    const { rerender } = render(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            fileId: fileA.id,
            filename: fileA.filename,
            mimeType: fileA.mime_type,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    await waitFor(() => expect(resolveSlowFirst).toBeDefined());

    // 在文件 A 请求未结束时切换到文件 B
    rerender(
      <TooltipProvider>
        <FilePreviewDialog
          file={{
            fileId: fileB.id,
            filename: fileB.filename,
            mimeType: fileB.mime_type,
          }}
          onOpenChange={() => undefined}
          open
          sessionId={SESSION.id}
        />
      </TooltipProvider>,
    );

    expect(firstAborted).toBe(true);
    expect(await screen.findByText("新文件 B 的权威内容")).toBeInTheDocument();

    // 即使旧 Promise 滞后 resolve，也不得污染文件 B 的视图
    resolveSlowFirst?.(
      jsonResponse(
        makeContentResponse({ content: "滞后旧内容不应出现", file: fileA }),
      ),
    );
    await waitFor(() => {
      expect(screen.getByText("新文件 B 的权威内容")).toBeInTheDocument();
      expect(screen.queryByText("滞后旧内容不应出现")).not.toBeInTheDocument();
    });
  });
});

describe("输入器解析状态微标、识图一键切换（方案 A2）与 5 张上限提示", () => {
  it("PDF 展示已解析/未提取微标且可点击预览；非识图模型展示温和条与一键切至识图模型，且永不禁用发送按钮", async () => {
    const readyPdf = makeSessionFile({
      extracted_text_bytes: 18_432,
      extraction_error_code: null,
      extraction_status: "ready",
      filename: "制度汇编.pdf",
      id: "file-ready-pdf",
      mime_type: "application/pdf",
      session_id: SESSION.id,
      size_bytes: 80_000,
    });
    const scannedPdf = makeSessionFile({
      extracted_text_bytes: null,
      extraction_error_code: "document_no_extractable_text",
      extraction_status: "failed",
      filename: "扫描发票.pdf",
      id: "file-scan-pdf",
      mime_type: "application/pdf",
      session_id: SESSION.id,
      size_bytes: 50_000,
    });
    const uploadedChart = makeSessionFile({
      filename: "chart.png",
      id: "file-chart-png",
      mime_type: "image/png",
      session_id: SESSION.id,
      size_bytes: 12_000,
    });

    const { submittedRuns } = installSessionFetch({
      onContent: (fileId) => {
        if (fileId === readyPdf.id) {
          return jsonResponse(
            makeContentResponse({
              content: "## 第 1 页\n报销制度正文",
              file: readyPdf,
              source_kind: "extracted_document",
            }),
          );
        }
        return jsonResponse({ detail: "document_no_extractable_text" }, 422);
      },
      onUpload: (file) => {
        if (file.name === readyPdf.filename) {
          return jsonResponse(readyPdf, 201);
        }
        if (file.name === scannedPdf.filename) {
          return jsonResponse(scannedPdf, 201);
        }
        return jsonResponse(uploadedChart, 201);
      },
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const fileInput = await screen.findByLabelText("选择会话文件");
    await user.upload(fileInput, [
      new File(["pdf-1"], readyPdf.filename, { type: "application/pdf" }),
      new File(["pdf-2"], scannedPdf.filename, { type: "application/pdf" }),
      new File(["png-1"], uploadedChart.filename, { type: "image/png" }),
    ]);

    // 1. 两个 PDF 分别展示「已解析」与「未提取」微标，图片展示 20×20 缩略图
    expect(await screen.findByText("已解析")).toBeInTheDocument();
    expect(screen.getByText("未提取")).toBeInTheDocument();
    expect(screen.getByTestId("composer-attachment-thumb")).toHaveAttribute(
      "src",
      `/api/v1/sessions/${SESSION.id}/files/${uploadedChart.id}/preview`,
    );

    // 点击已解析 PDF 芯片文件名即可直达 FilePreviewDialog 预览
    await user.click(
      screen.getByRole("button", { name: "预览附件：制度汇编.pdf" }),
    );
    const previewDialog = await screen.findByRole("dialog", {
      name: "制度汇编.pdf",
    });
    expect(
      await within(previewDialog).findByText("报销制度正文"),
    ).toBeInTheDocument();
    await user.click(
      within(previewDialog).getByRole("button", { name: "关闭预览" }),
    );

    // 点击已知提取失败的扫描 PDF 芯片，直接展示友好降级卡片且不发无谓报错
    await user.click(
      screen.getByRole("button", { name: "预览附件：扫描发票.pdf" }),
    );
    const scanDialog = await screen.findByRole("dialog", {
      name: "扫描发票.pdf",
    });
    expect(
      within(scanDialog).getByText("暂无法在线提取文档文本"),
    ).toBeInTheDocument();
    expect(within(scanDialog).getByText(/扫描件或空白文档/)).toBeInTheDocument();
    await user.click(
      within(scanDialog).getByRole("button", { name: "关闭预览" }),
    );

    // 2. 当前默认模型不支持识图 -> 展示紧凑琥珀条 + 一键切至识图模型，同时发送按钮保持可用（方案 A2）
    const visionBanner = screen.getByTestId("composer-vision-unsupported-banner");
    expect(visionBanner).toHaveAttribute("role", "status");
    expect(visionBanner).toHaveTextContent(
      "当前模型暂不支持识图，仍可直接发送，或切换至识图模型",
    );

    // 打开模型选择器浮层，验证浮层警告区内也保留了一键切至识图模型快捷按钮
    await user.click(screen.getByRole("button", { name: /选择模型：日常办公/ }));
    const modelPopover = screen.getByRole("dialog", { name: "选择对话模型" });
    expect(
      within(modelPopover).getByRole("button", {
        name: "一键切至「深度识图」",
      }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.type(screen.getByLabelText("对话草稿"), "帮我看看这张图和制度");
    expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();

    // 点击「一键切至「深度识图」」立即切换模型并隐藏警告条
    await user.click(
      within(visionBanner).getByRole("button", {
        name: "一键切至「深度识图」",
      }),
    );
    expect(
      screen.getByRole("button", { name: /选择模型：深度识图/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("composer-vision-unsupported-banner"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(submittedRuns).toHaveLength(1));
    expect(submittedRuns[0]).toMatchObject({
      model_profile_id: VISION_MODEL.id,
    });
  });

  it("目录无识图模型与上传超过 5 张图片时均给出温和提醒且不禁用发送按钮，草稿本地 objectURL 正确释放", async () => {
    installSessionFetch({
      models: {
        default_model_id: OFFICE_MODEL.id,
        items: [OFFICE_MODEL],
      },
    });
    let urlCounter = 0;
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockImplementation(() => `blob:draft-img-${++urlCounter}`);
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);

    const user = userEvent.setup();
    const view = renderApp("/chat");

    const fileInput = await screen.findByLabelText("选择任务文件");
    const sixImages = Array.from(
      { length: 6 },
      (_, idx) =>
        new File([`img-${idx}`], `shot-${idx + 1}.png`, { type: "image/png" }),
    );
    await user.upload(fileInput, sixImages);

    // 无可用识图模型提示（不展示切换按钮）
    const unsupportedBanner = await screen.findByTestId(
      "composer-vision-unsupported-banner",
    );
    expect(unsupportedBanner).toHaveTextContent("当前暂无可用识图模型");
    expect(
      within(unsupportedBanner).queryByRole("button", { name: /一键切至/ }),
    ).not.toBeInTheDocument();

    // 超 5 张图片温和提示
    const limitBanner = screen.getByTestId("composer-vision-limit-banner");
    expect(limitBanner).toHaveAttribute("role", "status");
    expect(limitBanner).toHaveTextContent(
      "单次对话最多同时识别 5 张图片，超出部分本次不会发送给识图模型（仍会保留为会话文件）",
    );

    // 方案 A2：输入草稿后发送按钮仍保持完全可用
    await user.type(screen.getByLabelText("对话草稿"), "仍要直接发送这 6 张图");
    expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();

    // 移除第 1 张图片后应立即 revokeObjectURL 释放内存
    expect(createObjectURL).toHaveBeenCalledTimes(6);
    await user.click(
      screen.getByRole("button", { name: "移除任务文件：shot-1.png" }),
    );
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:draft-img-1");
    // 剩 5 张图片后超限条自动消失
    expect(
      screen.queryByTestId("composer-vision-limit-banner"),
    ).not.toBeInTheDocument();

    // 卸载页面时剩余 5 个 objectURL 也必须全部释放
    view.unmount();
    expect(revokeObjectURL).toHaveBeenCalledTimes(6);
  });
});

describe("消息流原图灯箱、助手交付产物在线预览、文件抽屉预览与 SVG/HTML 安全隔离", () => {
  it("用户多图缩略图开灯箱、文档与交付产物在线预览、抽屉预览叠加无冲突，且 SVG/HTML 绝不直出 img", async () => {
    const img1 = makeSessionFile({
      filename: "invoice-1.png",
      id: "file-img-1",
      mime_type: "image/png",
      session_id: SESSION.id,
      size_bytes: 16_000,
    });
    const img2 = makeSessionFile({
      filename: "invoice-2.webp",
      id: "file-img-2",
      mime_type: "image/webp",
      session_id: SESSION.id,
      size_bytes: 18_000,
    });
    const userPdf = makeSessionFile({
      extracted_text_bytes: 9_600,
      extraction_error_code: null,
      extraction_status: "ready",
      filename: "报销单明细.pdf",
      id: "file-user-pdf",
      mime_type: "application/pdf",
      session_id: SESSION.id,
      size_bytes: 42_000,
    });
    const unsafeSvg = makeSessionFile({
      filename: "evil.svg",
      id: "file-unsafe-svg",
      mime_type: "image/svg+xml",
      session_id: SESSION.id,
      size_bytes: 1024,
    });
    const maskedSvg = makeSessionFile({
      filename: "masked.png",
      id: "file-masked-svg",
      mime_type: "image/svg+xml",
      session_id: SESSION.id,
      size_bytes: 768,
    });
    const unsafeHtml = makeSessionFile({
      filename: "attack.html",
      id: "file-unsafe-html",
      mime_type: "text/html",
      session_id: SESSION.id,
      size_bytes: 512,
    });
    const artifactMd = makeSessionFile({
      filename: "audit-report.md",
      id: "file-artifact-md",
      label: "合规核验报告",
      mime_type: "text/markdown",
      session_id: SESSION.id,
      size_bytes: 2048,
      source: "artifact",
    });
    const artifactPng = makeSessionFile({
      filename: "trend.png",
      id: "file-artifact-png",
      label: "费用趋势图",
      mime_type: "image/png",
      session_id: SESSION.id,
      size_bytes: 24_000,
      source: "artifact",
    });

    const completedRun = makeRun({
      id: "run-m34-done",
      session_id: SESSION.id,
      status: "completed",
    });
    const toolCall = makeToolCall({
      display_name: "生成合规核验报告",
      id: "call-report",
      model_name: "report_builder",
      ordinal: 1,
      tool_call_id: "tc-report",
    });

    installSessionFetch({
      files: [
        img1,
        img2,
        userPdf,
        unsafeSvg,
        maskedSvg,
        unsafeHtml,
        artifactMd,
        artifactPng,
      ],
      messages: [
        makeMessage({
          id: "msg-user-multimodal",
          parts: [
            { text: "请核对这两张发票截图和报销 PDF", type: "text" },
            {
              file_id: img1.id,
              filename: img1.filename,
              mime_type: img1.mime_type,
              size_bytes: img1.size_bytes,
              type: "file_ref",
            },
            {
              file_id: img2.id,
              filename: img2.filename,
              mime_type: img2.mime_type,
              size_bytes: img2.size_bytes,
              type: "file_ref",
            },
            {
              file_id: userPdf.id,
              filename: userPdf.filename,
              mime_type: userPdf.mime_type,
              size_bytes: userPdf.size_bytes,
              type: "file_ref",
            },
            {
              file_id: unsafeSvg.id,
              filename: unsafeSvg.filename,
              mime_type: unsafeSvg.mime_type,
              size_bytes: unsafeSvg.size_bytes,
              type: "file_ref",
            },
            {
              // 模拟 file_ref 缺少 mime_type 但会话文件缓存实为 image/svg+xml 的伪装场景
              file_id: maskedSvg.id,
              filename: maskedSvg.filename,
              mime_type: null,
              size_bytes: maskedSvg.size_bytes,
              type: "file_ref",
            },
            {
              file_id: unsafeHtml.id,
              filename: unsafeHtml.filename,
              mime_type: unsafeHtml.mime_type,
              size_bytes: unsafeHtml.size_bytes,
              type: "file_ref",
            },
          ],
          role: "user",
          run_id: null,
          sequence: 1,
          session_id: SESSION.id,
        }),
        makeMessage({
          id: "msg-assistant-artifacts",
          parts: [
            {
              artifact_id: null,
              artifacts: [
                makeArtifact({
                  file_id: artifactMd.id,
                  filename: artifactMd.filename,
                  label: artifactMd.label,
                  mime_type: artifactMd.mime_type,
                  size_bytes: artifactMd.size_bytes,
                }),
                makeArtifact({
                  file_id: artifactPng.id,
                  filename: artifactPng.filename,
                  label: artifactPng.label,
                  mime_type: artifactPng.mime_type,
                  size_bytes: artifactPng.size_bytes,
                }),
              ],
              output: null,
              status: "success",
              tool_call_id: "tc-report",
              type: "tool_result",
            },
          ],
          role: "assistant",
          run_id: completedRun.id,
          sequence: 2,
          session_id: SESSION.id,
        }),
      ],
      onContent: (fileId) => {
        if (fileId === artifactMd.id) {
          return jsonResponse(
            makeContentResponse({
              content: "# 合规核验结论\n全部发票真实有效。",
              file: artifactMd,
            }),
          );
        }
        return jsonResponse(
          makeContentResponse({
            content: "## 第 1 页\n报销总额 1280 元",
            file: userPdf,
            source_kind: "extracted_document",
          }),
        );
      },
      runs: [completedRun],
      toolCallDetail: () =>
        makeToolCallDetail({
          ...toolCall,
          artifacts: [
            makeArtifact({
              file_id: artifactMd.id,
              filename: artifactMd.filename,
              label: artifactMd.label,
              mime_type: artifactMd.mime_type,
              size_bytes: artifactMd.size_bytes,
            }),
          ],
        }),
      toolCalls: [toolCall],
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    // 1. 用户消息两张白名单图片以 2 列网格展示；SVG/HTML 绝不渲染 <img> 与预览按钮
    const imageGrid = await screen.findByTestId("message-image-grid");
    const gridImgs = within(imageGrid).getAllByRole("img");
    expect(gridImgs).toHaveLength(2);
    expect(gridImgs[0]).toHaveAttribute(
      "src",
      `/api/v1/sessions/${SESSION.id}/files/${img1.id}/preview`,
    );

    const messageRegion = screen.getByRole("region", { name: "消息历史" });
    expect(
      within(messageRegion).queryByRole("img", { name: /evil\.svg/i }),
    ).not.toBeInTheDocument();
    expect(
      within(messageRegion).queryByRole("img", { name: /masked\.png/i }),
    ).not.toBeInTheDocument();
    expect(
      within(messageRegion).queryByRole("button", { name: /预览.*evil\.svg/i }),
    ).not.toBeInTheDocument();
    expect(
      within(messageRegion).queryByRole("button", {
        name: /预览.*masked\.png/i,
      }),
    ).not.toBeInTheDocument();
    expect(
      within(messageRegion).queryByRole("button", {
        name: /预览.*attack\.html/i,
      }),
    ).not.toBeInTheDocument();
    expect(
      within(messageRegion).getByRole("button", { name: "下载文件：evil.svg" }),
    ).toBeInTheDocument();
    expect(
      within(messageRegion).getByRole("button", {
        name: "下载文件：masked.png",
      }),
    ).toBeInTheDocument();

    // 引用带自定义无后缀 label 的图片产物进入输入器芯片时，仍按原始 filename（trend.png）渲染 20×20 缩略图
    await user.click(screen.getByRole("button", { name: "添加附件" }));
    const attachPopover = screen.getByRole("dialog", {
      name: "引用本会话已有文件",
    });
    await user.click(
      within(attachPopover).getByRole("checkbox", { name: "费用趋势图" }),
    );
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("button", { name: "预览附件：费用趋势图" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("composer-attachment-thumb")).toHaveAttribute(
      "src",
      `/api/v1/sessions/${SESSION.id}/files/${artifactPng.id}/preview`,
    );

    // 点击用户图片缩略图打开原图灯箱
    await user.click(
      within(imageGrid).getByRole("button", { name: "查看大图：invoice-1.png" }),
    );
    const imgLightbox = await screen.findByRole("dialog", {
      name: "invoice-1.png",
    });
    expect(
      within(imgLightbox).getByRole("img", { name: "invoice-1.png" }),
    ).toHaveAttribute(
      "src",
      `/api/v1/sessions/${SESSION.id}/files/${img1.id}/preview`,
    );
    await user.click(
      within(imgLightbox).getByRole("button", { name: "关闭预览" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "invoice-1.png" }),
      ).not.toBeInTheDocument(),
    );

    // 2. 助手交付产物卡片：点击 Markdown 产物「在线预览」直接渲染报告内容
    const artifactSection = within(messageRegion).getByRole("region", {
      name: "交付产物",
    });
    expect(within(artifactSection).getByText("2 个文件")).toBeInTheDocument();
    await user.click(
      within(artifactSection).getByRole("button", {
        name: "在线预览：合规核验报告",
      }),
    );
    const mdDialog = await screen.findByRole("dialog", {
      name: "合规核验报告",
    });
    expect(
      await within(mdDialog).findByRole("heading", { name: "合规核验结论" }),
    ).toBeInTheDocument();
    await user.click(
      within(mdDialog).getByRole("button", { name: "关闭预览" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "合规核验报告" }),
      ).not.toBeInTheDocument(),
    );

    // 2.5 展开 ExecutionProgressAccordion 验证工具产物也支持直接预览
    const progressRegion = screen.getByRole("region", { name: "执行进度" });
    await user.click(
      within(progressRegion).getByRole("button", { name: /已完成 1 个步骤/ }),
    );
    const stepList = within(progressRegion).getByRole("list", {
      name: "工具调用步骤",
    });
    await user.click(
      within(stepList).getByRole("button", { name: /生成合规核验报告/ }),
    );
    await user.click(
      await within(progressRegion).findByRole("button", {
        name: "预览文件：合规核验报告",
      }),
    );
    const progressPreviewDialog = await screen.findByRole("dialog", {
      name: "合规核验报告",
    });
    expect(
      await within(progressPreviewDialog).findByRole("heading", {
        name: "合规核验结论",
      }),
    ).toBeInTheDocument();
    await user.click(
      within(progressPreviewDialog).getByRole("button", { name: "关闭预览" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "合规核验报告" }),
      ).not.toBeInTheDocument(),
    );

    // 3. 右侧 SessionFilesDrawer：展示图片缩略图、PDF 已解析微标、预览与下载双按钮，且与 FilePreviewDialog 层叠无冲突并正确归还焦点
    await user.click(screen.getByRole("button", { name: "会话文件（8）" }));
    const drawer = await screen.findByRole("dialog", { name: /会话文件/ });
    expect(
      within(drawer).getByRole("img", { name: "invoice-1.png" }),
    ).toHaveAttribute(
      "src",
      `/api/v1/sessions/${SESSION.id}/files/${img1.id}/preview`,
    );
    expect(
      within(drawer).queryByRole("img", { name: /evil\.svg/i }),
    ).not.toBeInTheDocument();
    expect(
      within(drawer).queryByRole("img", { name: /masked\.png/i }),
    ).not.toBeInTheDocument();
    expect(within(drawer).getByText("已解析")).toBeInTheDocument();

    // 在抽屉内点击 PDF「预览」打开顶层弹窗：抽屉主动让位卸载使页面仅存唯一模态层，
    // 关闭弹窗后抽屉无缝恢复且焦点精准归还至该文件的「预览」按钮（绝不跌落 document.body）
    const drawerPdfPreviewBtn = within(drawer).getByRole("button", {
      name: "预览文件：报销单明细.pdf",
    });
    await user.click(drawerPdfPreviewBtn);
    const pdfDialog = await screen.findByRole("dialog", {
      name: "报销单明细.pdf",
    });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(
      await within(pdfDialog).findByText("报销总额 1280 元"),
    ).toBeInTheDocument();
    await user.click(
      within(pdfDialog).getByRole("button", { name: "关闭预览" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "报销单明细.pdf" }),
      ).not.toBeInTheDocument(),
    );
    const restoredDrawer = screen.getByRole("dialog", { name: /会话文件/ });
    expect(restoredDrawer).toBeInTheDocument();
    await waitFor(() => {
      expect(
        within(restoredDrawer).getByRole("button", {
          name: "预览文件：报销单明细.pdf",
        }),
      ).toHaveFocus();
    });
  });
});
