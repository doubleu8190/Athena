import { Check, Copy } from "lucide-react";
import {
  isValidElement,
  memo,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { copyTextToClipboard } from "../lib/clipboard";
import { toast } from "./Toast";

const COPY_FEEDBACK_MS = 2_000;

function safeUrl(value: string | undefined): string | null {
  if (!value) {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  if (/^(https?:|mailto:|tel:)/i.test(trimmed)) {
    return trimmed;
  }
  // 双斜杠会被浏览器解释为跨站地址，必须按协议不可信拒绝。
  if (trimmed.startsWith("//")) {
    return null;
  }
  if (
    trimmed.startsWith("/") ||
    trimmed.startsWith("#") ||
    trimmed.startsWith("./") ||
    trimmed.startsWith("../")
  ) {
    return trimmed;
  }
  return null;
}

function externalLinkProps(href: string) {
  return /^https?:/i.test(href)
    ? { rel: "noopener noreferrer", target: "_blank" }
    : {};
}

/** 递归提取代码块内的纯文本内容，用于一键复制到剪贴板。 */
function extractNodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(extractNodeText).join("");
  }
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return extractNodeText(node.props.children);
  }
  return "";
}

/** 从 react-markdown 生成的 <code> 子元素类名（language-xxx）提取代码语言标签。 */
function extractCodeLanguage(node: ReactNode): string | null {
  if (isValidElement<{ className?: string }>(node)) {
    const match = /language-([^\s]+)/.exec(node.props.className ?? "");
    return match?.[1] ?? null;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      const matched = extractCodeLanguage(child);
      if (matched) {
        return matched;
      }
    }
  }
  return null;
}

function MarkdownCodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);
  const language = extractCodeLanguage(children);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    // 去除 Markdown 围栏代码块末尾自带的单个换行符，方便直接粘贴执行。
    const rawText = extractNodeText(children).replace(/\n$/, "");
    const ok = await copyTextToClipboard(rawText);
    if (!ok) {
      toast.error("当前环境无法自动复制，请手动选中代码复制");
      return;
    }
    setCopied(true);
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setCopied(false);
    }, COPY_FEEDBACK_MS);
  };

  return (
    <div className="markdown-code-block my-3 overflow-hidden rounded-xl border border-border-default bg-subtle">
      <div className="flex items-center justify-between gap-2 border-b border-border-subtle bg-muted/80 px-3 py-1.5 text-xs text-text-secondary">
        <span className="font-mono text-[11px] font-medium tracking-wide text-text-tertiary uppercase">
          {language ?? "代码"}
        </span>
        <button
          aria-label={copied ? "已复制" : "复制代码"}
          className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-text-secondary transition-colors hover:bg-canvas hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
          data-compact-control="true"
          onClick={() => void handleCopy()}
          type="button"
        >
          {copied ? (
            <>
              <Check aria-hidden="true" className="h-3.5 w-3.5 text-success" />
              <span className="text-success">已复制</span>
            </>
          ) : (
            <>
              <Copy aria-hidden="true" className="h-3.5 w-3.5" />
              <span>复制代码</span>
            </>
          )}
        </button>
      </div>
      <pre className="m-0 overflow-x-auto border-0 bg-transparent p-3.5 font-mono text-[13px] leading-relaxed text-text-primary">
        {children}
      </pre>
    </div>
  );
}

// 将插件数组与组件映射提升至模块级常量，避免每次渲染创建新引用触发 ReactMarkdown 重解析 AST。
const MARKDOWN_REMARK_PLUGINS = [remarkGfm];

const MARKDOWN_COMPONENTS: Components = {
  a({ children, href }) {
    const safeHref = safeUrl(href);
    if (!safeHref) {
      return <span>{children}</span>;
    }
    return (
      <a href={safeHref} {...externalLinkProps(safeHref)}>
        {children}
      </a>
    );
  },
  img({ alt, src }) {
    const safeSrc = safeUrl(src);
    const label = alt?.trim() || "图片";
    if (!safeSrc) {
      return <span className="markdown-image-fallback">{label}</span>;
    }
    // 图片不直接加载；保留为可访问的外部链接，避免自动请求和追踪。
    return (
      <a
        className="markdown-image-link"
        href={safeSrc}
        rel="noopener noreferrer"
        target="_blank"
      >
        图片：{label}
      </a>
    );
  },
  pre({ children }) {
    return <MarkdownCodeBlock>{children}</MarkdownCodeBlock>;
  },
  table({ children }) {
    // 宽表格包裹独立横向滚动容器，避免移动端或窄栏下撑破消息视口。
    return (
      <div className="markdown-table-wrap my-3 w-full overflow-x-auto rounded-lg border border-border-default">
        <table>{children}</table>
      </div>
    );
  },
};

export interface MarkdownContentProps {
  className?: string;
  text: string;
}

// 记忆化渲染安全 Markdown，文本与样式未变时跳过昂贵的 AST 解析与子树比对。
export const MarkdownContent = memo(function MarkdownContent({
  className,
  text,
}: MarkdownContentProps) {
  return (
    <div className={`markdown-content${className ? ` ${className}` : ""}`}>
      <ReactMarkdown
        components={MARKDOWN_COMPONENTS}
        remarkPlugins={MARKDOWN_REMARK_PLUGINS}
        skipHtml
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
