import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import type { Components } from 'react-markdown'

const components: Components = {
  code({ className, children, ...props }) {
    const isInline = !className
    if (isInline) {
      return (
        <code className="bg-bg-elevated px-1.5 py-0.5 rounded text-xs text-accent font-mono" {...props}>
          {children}
        </code>
      )
    }
    return (
      <div className="relative group my-3">
        <pre className="bg-bg-elevated p-3 rounded-xl overflow-x-auto text-xs font-mono leading-relaxed shadow-soft">
          <code className={className} {...props}>
            {children}
          </code>
        </pre>
      </div>
    )
  },
  a({ href, children, ...props }) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-accent hover:underline"
        {...props}
      >
        {children}
      </a>
    )
  },
  table({ children }) {
    return (
      <div className="overflow-x-auto my-3 shadow-soft rounded-xl">
        <table className="w-full text-sm border-collapse">
          {children}
        </table>
      </div>
    )
  },
  th({ children }) {
    return (
      <th className="border-b border-border-subtle bg-bg-elevated px-3 py-2 text-left text-xs font-semibold text-text-primary">
        {children}
      </th>
    )
  },
  td({ children }) {
    return (
      <td className="border-b border-border-subtle px-3 py-2 text-xs text-text-secondary">
        {children}
      </td>
    )
  },
  h1({ children }) {
    return <h1 className="text-xl font-bold text-text-primary mt-6 mb-3">{children}</h1>
  },
  h2({ children }) {
    return <h2 className="text-lg font-bold text-text-primary mt-5 mb-2">{children}</h2>
  },
  h3({ children }) {
    return <h3 className="text-base font-semibold text-text-primary mt-4 mb-2">{children}</h3>
  },
  h4({ children }) {
    return <h4 className="text-sm font-semibold text-text-primary mt-3 mb-1">{children}</h4>
  },
  h5({ children }) {
    return <h5 className="text-sm font-medium text-text-primary mt-3 mb-1">{children}</h5>
  },
  h6({ children }) {
    return <h6 className="text-xs font-medium text-text-secondary mt-3 mb-1">{children}</h6>
  },
  blockquote({ children }) {
    return (
      <blockquote className="border-l-[3px] border-accent/40 pl-4 my-3 text-text-secondary text-sm italic">
        {children}
      </blockquote>
    )
  },
  ul({ children }) {
    return <ul className="list-disc list-inside my-2 space-y-1 text-sm">{children}</ul>
  },
  ol({ children }) {
    return <ol className="list-decimal list-inside my-2 space-y-1 text-sm">{children}</ol>
  },
  li({ children }) {
    return <li className="text-text-secondary">{children}</li>
  },
  hr() {
    return <hr className="my-4 border-border-subtle" />
  },
  p({ children }) {
    return <p className="my-1.5 text-sm leading-relaxed">{children}</p>
  },
  strong({ children }) {
    return <strong className="font-semibold text-text-primary">{children}</strong>
  },
  em({ children }) {
    return <em className="italic">{children}</em>
  },
  img({ src, alt }) {
    return (
      <img
        src={src}
        alt={alt}
        className="max-w-full rounded-xl my-2 shadow-soft"
        loading="lazy"
      />
    )
  },
  del({ children }) {
    return <del className="line-through text-text-muted">{children}</del>
  },
}

interface MarkdownRendererProps {
  content: string
}

export default function MarkdownRenderer({ content }: MarkdownRendererProps) {
  if (!content) return null

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={components}
    >
      {content}
    </ReactMarkdown>
  )
}
