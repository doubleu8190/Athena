import type { ReactNode } from "react";

import { Icon, type IconName } from "./Icons";

interface EmptyStateProps {
  action?: ReactNode;
  compact?: boolean;
  description: string;
  icon?: IconName;
  kicker?: string;
  title: string;
}

export function EmptyState({
  action,
  compact = false,
  description,
  icon = "spark",
  kicker,
  title,
}: EmptyStateProps) {
  return (
    <section
      className={`state-panel${compact ? " state-panel--compact" : ""}`}
      aria-label={title}
    >
      <span className="state-panel-icon" aria-hidden="true">
        <Icon name={icon} size={compact ? 20 : 25} />
      </span>
      <div className="state-panel-copy">
        {kicker ? <span className="state-panel-kicker">{kicker}</span> : null}
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {action ? <div className="state-panel-action">{action}</div> : null}
    </section>
  );
}

