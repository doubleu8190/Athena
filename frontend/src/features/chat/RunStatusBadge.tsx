import { RUN_STATUS_LABELS, type RunStatus } from "./model";

interface RunStatusBadgeProps {
  status: RunStatus;
}

export function RunStatusBadge({ status }: RunStatusBadgeProps) {
  return (
    <span className={`run-status run-status--${status}`}>
      <span aria-hidden="true" />
      {RUN_STATUS_LABELS[status]}
    </span>
  );
}

