import {
  Bold,
  Code2,
  Columns2,
  Eye,
  FileUp,
  Heading2,
  List,
  Loader2,
  Pencil,
  Table2,
  type LucideIcon,
} from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { MarkdownContent } from "./MarkdownContent";
import { cn } from "./utils";

export type MarkdownEditorViewMode = "edit" | "split" | "preview";
export type MarkdownFormatAction =
  | "heading"
  | "bold"
  | "list"
  | "code"
  | "table";

export interface MarkdownSplitEditorProps {
  /** 关联外部 <label htmlFor={id}> 与无障碍定位的稳定 ID。 */
  id: string;
  /** 写入原生 <textarea aria-label={label}>，保障无障碍与既有测试查询兼容。 */
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Unicode 码点上限（使用 Array.from(value).length 统计，避免代理对计数偏差）。 */
  maxLength?: number;
  error?: string | null;
  describedBy?: string;
  rows?: number;
  minHeightClassName?: string;
  /** 提供时在工具栏右侧渲染「导入 .md」按钮，校验 .md 后缀后回调上层处理。 */
  onImportMarkdownFile?: (file: File) => void;
  importingMarkdown?: boolean;
  importTriggerLabel?: string;
  headerExtra?: ReactNode;
  className?: string;
}

interface MarkdownInsertResult {
  nextValue: string;
  selectionEnd: number;
  selectionStart: number;
}

interface ViewTabOption {
  icon: LucideIcon;
  label: string;
  value: MarkdownEditorViewMode;
}

const DESKTOP_BREAKPOINT_QUERY = "(min-width: 1024px)";

const DESKTOP_VIEW_TABS: readonly ViewTabOption[] = [
  { icon: Pencil, label: "仅编辑", value: "edit" },
  { icon: Columns2, label: "分栏", value: "split" },
  { icon: Eye, label: "仅预览", value: "preview" },
];

const NARROW_VIEW_TABS: readonly ViewTabOption[] = [
  { icon: Pencil, label: "编辑", value: "edit" },
  { icon: Eye, label: "预览", value: "preview" },
];

const TOOLBAR_ACTIONS: ReadonlyArray<{
  action: MarkdownFormatAction;
  icon: LucideIcon;
  label: string;
}> = [
  { action: "heading", icon: Heading2, label: "插入二级标题" },
  { action: "bold", icon: Bold, label: "加粗" },
  { action: "list", icon: List, label: "无序列表" },
  { action: "code", icon: Code2, label: "代码块" },
  { action: "table", icon: Table2, label: "插入表格" },
];

const MARKDOWN_TABLE_TEMPLATE = [
  "| 列 1 | 列 2 | 列 3 |",
  "| --- | --- | --- |",
  "| 内容 1 | 内容 2 | 内容 3 |",
  "| 内容 4 | 内容 5 | 内容 6 |",
].join("\n");

/**
 * 纯函数计算轻量 Markdown 排版工具栏的插入结果与恢复选区。
 * 在光标或选区位置精准插入语法并选中占位文本，便于用户直接键入覆盖。
 */
export function applyMarkdownFormat(
  value: string,
  rawStart: number,
  rawEnd: number,
  action: MarkdownFormatAction,
): MarkdownInsertResult {
  const start = Math.max(0, Math.min(rawStart, value.length));
  const end = Math.max(start, Math.min(rawEnd, value.length));
  const before = value.slice(0, start);
  const selected = value.slice(start, end);
  const after = value.slice(end);
  const needLeadingNewline = before.length > 0 && !before.endsWith("\n");
  const leadingNewline = needLeadingNewline ? "\n" : "";

  switch (action) {
    case "heading": {
      if (!selected) {
        const placeholder = "二级标题";
        const inserted = `${leadingNewline}## ${placeholder}`;
        const contentStart = start + leadingNewline.length + 3;
        return {
          nextValue: `${before}${inserted}${after}`,
          selectionEnd: contentStart + placeholder.length,
          selectionStart: contentStart,
        };
      }
      const inserted = `${leadingNewline}## ${selected}`;
      const contentStart = start + leadingNewline.length + 3;
      return {
        nextValue: `${before}${inserted}${after}`,
        selectionEnd: contentStart + selected.length,
        selectionStart: contentStart,
      };
    }

    case "bold": {
      const target = selected || "文本";
      const inserted = `**${target}**`;
      const contentStart = start + 2;
      return {
        nextValue: `${before}${inserted}${after}`,
        selectionEnd: contentStart + target.length,
        selectionStart: contentStart,
      };
    }

    case "list": {
      if (!selected) {
        const placeholder = "列表项";
        const inserted = `${leadingNewline}- ${placeholder}`;
        const contentStart = start + leadingNewline.length + 2;
        return {
          nextValue: `${before}${inserted}${after}`,
          selectionEnd: contentStart + placeholder.length,
          selectionStart: contentStart,
        };
      }
      const transformed = selected
        .split("\n")
        .map((line) => (line.startsWith("- ") ? line : `- ${line}`))
        .join("\n");
      const inserted = `${leadingNewline}${transformed}`;
      const contentStart = start + leadingNewline.length;
      return {
        nextValue: `${before}${inserted}${after}`,
        selectionEnd: contentStart + transformed.length,
        selectionStart: contentStart,
      };
    }

    case "code": {
      const target = selected || "代码内容";
      const trailingNewline = after.length > 0 && !after.startsWith("\n") ? "\n" : "";
      const prefix = `${leadingNewline}\`\`\`\n`;
      const inserted = `${prefix}${target}\n\`\`\`${trailingNewline}`;
      const contentStart = start + prefix.length;
      return {
        nextValue: `${before}${inserted}${after}`,
        selectionEnd: contentStart + target.length,
        selectionStart: contentStart,
      };
    }

    case "table": {
      // 表格块前后保留换行，确保 GFM 表格在任意插入位置都能正确解析渲染。
      const blockPrefix =
        before.length === 0
          ? ""
          : before.endsWith("\n\n")
            ? ""
            : before.endsWith("\n")
              ? "\n"
              : "\n\n";
      const blockSuffix =
        after.length === 0 || after.startsWith("\n") ? "" : "\n";
      const inserted = `${blockPrefix}${MARKDOWN_TABLE_TEMPLATE}${blockSuffix}`;
      const firstHeaderOffset = blockPrefix.length + 2;
      return {
        nextValue: `${before}${inserted}${after}`,
        selectionEnd: start + firstHeaderOffset + 3,
        selectionStart: start + firstHeaderOffset,
      };
    }
  }
}

function readDesktopViewport(): boolean {
  if (typeof window === "undefined") {
    return true;
  }
  if (typeof window.matchMedia === "function") {
    return window.matchMedia(DESKTOP_BREAKPOINT_QUERY).matches;
  }
  return window.innerWidth >= 1024;
}

/**
 * 监听 >= 1024px 桌面端断点，控制三态（仅编辑/分栏/仅预览）与窄屏二态（编辑/预览）自动切换。
 */
function useIsDesktopViewport(): boolean {
  const [isDesktop, setIsDesktop] = useState<boolean>(readDesktopViewport);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    if (typeof window.matchMedia === "function") {
      const mql = window.matchMedia(DESKTOP_BREAKPOINT_QUERY);
      setIsDesktop(mql.matches);
      const handleChange = (event: MediaQueryListEvent) => {
        setIsDesktop(event.matches);
      };
      if (typeof mql.addEventListener === "function") {
        mql.addEventListener("change", handleChange);
        return () => mql.removeEventListener("change", handleChange);
      }
      // 兼容旧版 Safari MediaQueryList 订阅 API。
      mql.addListener(handleChange);
      return () => mql.removeListener(handleChange);
    }
    const handleResize = () => {
      setIsDesktop(window.innerWidth >= 1024);
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  return isDesktop;
}

export function MarkdownSplitEditor({
  className,
  describedBy,
  disabled = false,
  error,
  headerExtra,
  id,
  importTriggerLabel = "导入 .md",
  importingMarkdown = false,
  label,
  maxLength,
  minHeightClassName = "min-h-[260px]",
  onChange,
  onImportMarkdownFile,
  placeholder,
  rows = 12,
  value,
}: MarkdownSplitEditorProps) {
  const isDesktop = useIsDesktopViewport();
  // 桌面端默认分栏（左编辑右预览）；窄屏下自动折叠为「编辑 / 预览」Tabs，切回宽屏无损恢复分栏偏好。
  const [viewMode, setViewMode] = useState<MarkdownEditorViewMode>("split");
  const [localImportError, setLocalImportError] = useState<string | null>(null);

  const panelId = useId();
  const errorId = useId();
  const importErrorId = useId();
  const tabIdPrefix = useId();

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const tabButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const pendingSelectionRef = useRef<{ end: number; start: number } | null>(
    null,
  );

  const effectiveMode: MarkdownEditorViewMode = isDesktop
    ? viewMode
    : viewMode === "preview"
      ? "preview"
      : "edit";
  const viewTabs = isDesktop ? DESKTOP_VIEW_TABS : NARROW_VIEW_TABS;
  const activeTabId = `${tabIdPrefix}-${effectiveMode}`;

  const codePoints = useMemo(() => Array.from(value).length, [value]);
  const isOverLimit = maxLength !== undefined && codePoints > maxLength;
  const hasError = Boolean(error) || isOverLimit;
  const showEditorPane = effectiveMode !== "preview";
  const showPreviewPane = effectiveMode !== "edit";
  const toolbarDisabled = disabled || importingMarkdown || !showEditorPane;

  // 在 React 提交新 value 后同步恢复光标/选区并保持编辑器焦点，避免受控输入重置光标到末尾。
  useLayoutEffect(() => {
    const pending = pendingSelectionRef.current;
    const textarea = textareaRef.current;
    if (!pending || !textarea || !showEditorPane) {
      return;
    }
    pendingSelectionRef.current = null;
    textarea.focus();
    const maxPos = textarea.value.length;
    textarea.setSelectionRange(
      Math.min(pending.start, maxPos),
      Math.min(pending.end, maxPos),
    );
  }, [showEditorPane, value]);

  const handleToolbarAction = (action: MarkdownFormatAction) => {
    if (toolbarDisabled) {
      return;
    }
    const textarea = textareaRef.current;
    const selectionStart = textarea?.selectionStart ?? value.length;
    const selectionEnd = textarea?.selectionEnd ?? value.length;
    const result = applyMarkdownFormat(
      value,
      selectionStart,
      selectionEnd,
      action,
    );
    // 文本无变更时（例如对已有 "- " 列表行再次点击无序列表）React 不会触发依赖 value 的 useLayoutEffect；
    // 直接同步恢复焦点与选区并清空 pendingSelectionRef，防止残留 ref 劫持用户后续打字光标。
    if (result.nextValue === value) {
      pendingSelectionRef.current = null;
      if (textarea) {
        textarea.focus();
        const maxPos = textarea.value.length;
        textarea.setSelectionRange(
          Math.min(result.selectionStart, maxPos),
          Math.min(result.selectionEnd, maxPos),
        );
      }
      return;
    }
    pendingSelectionRef.current = {
      end: result.selectionEnd,
      start: result.selectionStart,
    };
    onChange(result.nextValue);
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    if (
      event.key !== "ArrowLeft" &&
      event.key !== "ArrowRight" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    event.preventDefault();
    const count = viewTabs.length;
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % count;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + count) % count;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = count - 1;
    }
    const nextTab = viewTabs[nextIndex];
    if (!nextTab) {
      return;
    }
    setViewMode(nextTab.value);
    tabButtonRefs.current[nextTab.value]?.focus();
  };

  const handleFileInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selectedFile = event.currentTarget.files?.[0] ?? null;
    // 清空 input 值，支持用户修复后重复选择同名 .md 文件再次触发导入。
    event.currentTarget.value = "";
    if (!selectedFile || !onImportMarkdownFile) {
      return;
    }
    if (!selectedFile.name.toLowerCase().endsWith(".md")) {
      setLocalImportError("仅支持导入 .md 扩展名的 Markdown 文件。");
      return;
    }
    if (selectedFile.size <= 0) {
      setLocalImportError("所选 Markdown 文件为空，请选择包含正文的 .md 文件。");
      return;
    }
    setLocalImportError(null);
    onImportMarkdownFile(selectedFile);
  };

  const computedDescribedBy = [
    describedBy,
    error ? errorId : undefined,
    localImportError ? importErrorId : undefined,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={cn("min-w-0 w-full", className)}
      data-markdown-editor-mode={effectiveMode}
    >
      <div
        className={cn(
          "overflow-hidden rounded-xl border bg-canvas shadow-xs transition-colors",
          hasError
            ? "border-danger focus-within:ring-2 focus-within:ring-danger/20"
            : "border-border-default focus-within:border-ring-focus focus-within:ring-2 focus-within:ring-ring-focus/20",
          disabled && "bg-subtle/60",
        )}
      >
        {/* 顶部工具栏：左侧排版动作，右侧视图切换、扩展槽与快捷 .md 导入 */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle bg-subtle px-2.5 py-2">
          <div
            aria-label="Markdown 排版工具栏"
            className="flex flex-wrap items-center gap-1"
            role="toolbar"
          >
            {TOOLBAR_ACTIONS.map(({ action, icon: ActionIcon, label: actionLabel }) => (
              <button
                aria-label={actionLabel}
                className={cn(
                  "inline-flex h-7 items-center gap-1 rounded-md border border-transparent px-2 text-xs font-medium text-text-secondary transition-colors",
                  "hover:border-border-default hover:bg-canvas hover:text-text-primary",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                  "disabled:pointer-events-none disabled:opacity-45",
                )}
                data-compact-control="true"
                disabled={toolbarDisabled}
                key={action}
                onClick={() => handleToolbarAction(action)}
                title={actionLabel}
                type="button"
              >
                <ActionIcon aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                <span className="hidden sm:inline">{actionLabel}</span>
              </button>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {headerExtra ? (
              <div className="flex items-center gap-1.5">{headerExtra}</div>
            ) : null}

            {onImportMarkdownFile ? (
              <>
                <input
                  accept=".md,text/markdown"
                  aria-label={importTriggerLabel}
                  className="sr-only"
                  disabled={disabled || importingMarkdown}
                  onChange={handleFileInputChange}
                  ref={fileInputRef}
                  tabIndex={-1}
                  type="file"
                />
                <button
                  className={cn(
                    "inline-flex h-7 items-center gap-1.5 rounded-md border border-border-default bg-canvas px-2.5 text-xs font-medium text-text-primary transition-colors",
                    "hover:border-border-strong hover:bg-muted/70",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                    "disabled:pointer-events-none disabled:opacity-50",
                  )}
                  data-compact-control="true"
                  disabled={disabled || importingMarkdown}
                  onClick={() => fileInputRef.current?.click()}
                  type="button"
                >
                  {importingMarkdown ? (
                    <Loader2
                      aria-hidden="true"
                      className="h-3.5 w-3.5 shrink-0 animate-spin motion-reduce:animate-none"
                    />
                  ) : (
                    <FileUp aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                  )}
                  <span>
                    {importingMarkdown ? "正在解析…" : importTriggerLabel}
                  </span>
                </button>
              </>
            ) : null}

            <div
              aria-label="编辑器视图模式"
              className="inline-flex items-center gap-0.5 rounded-lg border border-border-default bg-muted/80 p-0.5"
              role="tablist"
            >
              {viewTabs.map((tab, index) => {
                const TabIcon = tab.icon;
                const selected = effectiveMode === tab.value;
                return (
                  <button
                    aria-controls={panelId}
                    aria-selected={selected}
                    className={cn(
                      "inline-flex h-6 cursor-pointer items-center gap-1 rounded-md px-2 text-xs font-medium whitespace-nowrap transition-colors select-none",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                      selected
                        ? "bg-canvas text-brand shadow-xs"
                        : "text-text-secondary hover:text-text-primary",
                    )}
                    data-compact-control="true"
                    id={`${tabIdPrefix}-${tab.value}`}
                    key={tab.value}
                    onClick={() => setViewMode(tab.value)}
                    onKeyDown={(event) => handleTabKeyDown(event, index)}
                    ref={(node) => {
                      tabButtonRefs.current[tab.value] = node;
                    }}
                    role="tab"
                    tabIndex={selected ? 0 : -1}
                    type="button"
                  >
                    <TabIcon aria-hidden="true" className="h-3 w-3 shrink-0" />
                    <span>{tab.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* 编辑 / 预览主体：分栏模式下桌面端左右等宽，窄屏自动切单栏 Tabs */}
        <div
          aria-labelledby={activeTabId}
          className={cn(
            "grid min-w-0",
            effectiveMode === "split"
              ? "grid-cols-1 divide-y divide-border-subtle lg:grid-cols-2 lg:divide-x lg:divide-y-0"
              : "grid-cols-1",
          )}
          id={panelId}
          role="tabpanel"
        >
          {showEditorPane ? (
            <div className="min-w-0">
              <textarea
                aria-describedby={computedDescribedBy || undefined}
                aria-invalid={hasError ? true : undefined}
                aria-label={label}
                className={cn(
                  "block w-full resize-y border-0 bg-canvas px-3.5 py-3 font-mono text-[13px] leading-relaxed text-text-primary",
                  "placeholder:text-text-placeholder",
                  "focus-visible:outline-hidden",
                  "disabled:cursor-not-allowed disabled:bg-subtle disabled:text-text-tertiary",
                  minHeightClassName,
                )}
                disabled={disabled || importingMarkdown}
                id={id}
                onChange={(event) => {
                  // 用户直接键盘输入时立即清空待恢复选区，确保普通打字绝不被历史工具栏选区劫持。
                  pendingSelectionRef.current = null;
                  onChange(event.target.value);
                }}
                placeholder={placeholder}
                ref={textareaRef}
                rows={rows}
                spellCheck={false}
                value={value}
              />
            </div>
          ) : null}

          {showPreviewPane ? (
            <div
              aria-label="Markdown 实时预览"
              className={cn(
                "min-w-0 overflow-y-auto bg-subtle/40 px-4 py-3 text-sm text-text-primary",
                minHeightClassName,
              )}
              data-testid="markdown-split-preview"
            >
              {value.trim().length > 0 ? (
                <MarkdownContent text={value} />
              ) : (
                <p className="py-6 text-center text-xs leading-relaxed text-text-tertiary">
                  实时预览区：开始输入 Markdown 正文，此处将同步渲染标题、列表、表格与代码块效果。
                </p>
              )}
            </div>
          ) : null}
        </div>

        {/* 底部字数与状态栏 */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle bg-subtle/70 px-3 py-1.5 text-xs">
          <span
            className={cn(
              "text-text-tertiary",
              isOverLimit && "font-medium text-danger",
            )}
          >
            {isOverLimit
              ? "已超出最大字符限制，请精简后再提交"
              : "支持标准 Markdown 与表格语法，预览区已启用安全防注入过滤"}
          </span>
          <span
            className={cn(
              "tabular-nums",
              isOverLimit ? "font-semibold text-danger" : "text-text-tertiary",
            )}
            data-over-limit={isOverLimit ? "true" : "false"}
            data-testid="markdown-char-count"
          >
            {maxLength !== undefined
              ? `${codePoints.toLocaleString()} / ${maxLength.toLocaleString()} 字符`
              : `${codePoints.toLocaleString()} 字符`}
          </span>
        </div>
      </div>

      {localImportError ? (
        <p
          className="mt-1.5 text-xs font-medium text-danger"
          id={importErrorId}
          role="alert"
        >
          {localImportError}
        </p>
      ) : null}

      {error ? (
        <p
          className="mt-1.5 text-xs font-medium text-danger"
          id={errorId}
          role="alert"
        >
          {error}
        </p>
      ) : null}
    </div>
  );
}
