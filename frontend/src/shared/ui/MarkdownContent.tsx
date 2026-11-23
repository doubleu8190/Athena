import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

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
