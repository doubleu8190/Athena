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
    <div className="workspace-error" role="alert">
      <Icon name="shield" size={17} />
      <span>{message}</span>
      {onRetry ? (
        <button className="text-button" onClick={onRetry} type="button">
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

