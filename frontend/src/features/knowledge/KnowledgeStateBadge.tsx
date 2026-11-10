import {
  documentStateLabel,
  type KnowledgeDocumentState,
} from "./model";

interface KnowledgeStateBadgeProps {
  state: KnowledgeDocumentState | null;
}

export function KnowledgeStateBadge({
  state,
}: KnowledgeStateBadgeProps) {
  return (
    <span
      className={`knowledge-state-badge${
        state ? ` knowledge-state-badge--${state}` : ""
      }`}
    >
      <span aria-hidden="true" />
      {documentStateLabel(state)}
    </span>
  );
}
