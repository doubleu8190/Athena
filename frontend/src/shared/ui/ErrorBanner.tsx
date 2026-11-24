import { Icon } from "./Icons";

interface ErrorBannerProps {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
}

export function ErrorBanner({
  message,
  onRetry,
  retryLabel = "重试",
}: ErrorBannerProps) {
  return (
    <div
      className="workspace-error flex items-start gap-[9px] rounded-[9px] border border-danger-border bg-danger-bg px-3 py-[11px] text-xs leading-relaxed text-danger"
      role="alert"
    >
      <Icon className="mt-px shrink-0" name="shield" size={17} />
      <span className="flex-1">{message}</span>
      {onRetry ? (
        <button className="text-button" onClick={onRetry} type="button">
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

