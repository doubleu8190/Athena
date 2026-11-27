import { cn } from "../../shared/ui";
import {
  documentStateLabel,
  type KnowledgeDocumentState,
} from "./model";

interface KnowledgeStateBadgeProps {
  state: KnowledgeDocumentState | null;
}

// 四种文档版本状态对应的文字与圆点令牌，完整对齐 tokens.css 语义色板。
const STATE_STYLE_MAP: Record<
  KnowledgeDocumentState,
  { dot: string; text: string }
> = {
  failed: { dot: "bg-danger", text: "text-danger" },
  processing: {
    dot: "bg-warning shadow-[0_0_0_3px_var(--warning-bg)]",
    text: "text-warning",
  },
  queued: { dot: "bg-text-tertiary", text: "text-text-secondary" },
  ready: { dot: "bg-success", text: "text-success" },
};

export function KnowledgeStateBadge({
  state,
}: KnowledgeStateBadgeProps) {
  const styles = state ? STATE_STYLE_MAP[state] : null;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-xs font-semibold",
        styles?.text ?? "text-text-secondary",
      )}
      data-state={state ?? "unknown"}
    >
      <span
        aria-hidden="true"
        className={cn(
          "h-1.5 w-1.5 shrink-0 rounded-full",
          styles?.dot ?? "bg-text-placeholder",
        )}
      />
      {documentStateLabel(state)}
    </span>
  );
}
