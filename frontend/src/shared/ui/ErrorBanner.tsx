import { InlineAlert } from "./InlineAlert";

interface ErrorBannerProps {
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
}

/**
 * 紧凑型错误提示横幅，统一复用 InlineAlert 的 danger 变体与 role="alert" 无障碍语义。
 */
export function ErrorBanner({
  message,
  onRetry,
  retryLabel = "重试",
}: ErrorBannerProps) {
  return (
    <InlineAlert
      action={
        onRetry ? (
          <button className="text-button" onClick={onRetry} type="button">
            {retryLabel}
          </button>
        ) : undefined
      }
      className="workspace-error gap-[9px] rounded-[9px] px-3 py-[11px] text-xs leading-relaxed"
      variant="danger"
    >
      {message}
    </InlineAlert>
  );
}
