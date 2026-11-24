import { AlertCircle, RotateCcw } from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Button,
  InlineAlert,
} from "../../shared/ui";
import type { Run } from "./model";

export const RETRY_RISK_WARNING =
  "整个任务将从头执行，可能重复此前已成功的操作；原操作结果未知时也可能已生效。";

const FAILURE_REASON_COPY: Record<string, string> = {
  execution_timeout: "任务处理耗时超出上限，您可以稍后重新执行。",
  llm_provider_error: "智能模型服务暂时波动，请稍后重新执行。",
  model_error: "智能模型服务暂时波动，请稍后重新执行。",
  model_unavailable: "当前所选模型暂时不可用，请稍后重试或切换其他模型。",
  rate_limited: "当前请求过于频繁，请稍候片刻后再重新执行。",
  run_context_safety_stop: "对话上下文已达安全上限，本次任务已中止处理。",
  skill_execution_failed: "技能脚本未能顺利完成，请核对输入文件或参数后重试。",
  timeout: "任务处理耗时超出上限，您可以稍后重新执行。",
  tool_execution_failed: "部分业务操作执行失败，导致本次任务未能完成。",
};

/** 将底层安全错误码翻译为面向企业员工的温和业务中文原因。 */
export function runFailureReason(safeErrorCode: string | null): string {
  if (safeErrorCode && Object.hasOwn(FAILURE_REASON_COPY, safeErrorCode)) {
    return FAILURE_REASON_COPY[safeErrorCode];
  }
  return "本次任务未能顺利完成，您可以点击下方按钮重新执行。";
}

interface FailedRunCardProps {
  canRetry: boolean;
  commandPending: boolean;
  onOpenRetry: () => void;
  run: Run;
}

/** 失败态柔和红框卡片：展示业务化失败原因并提供「重新执行」入口。 */
export function FailedRunCard({
  canRetry,
  commandPending,
  onOpenRetry,
  run,
}: FailedRunCardProps) {
  return (
    <section
      aria-labelledby="failed-run-card-title"
      className="rounded-xl border border-danger-border bg-danger-bg/60 p-4 shadow-xs"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-start gap-2.5">
          <AlertCircle
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 shrink-0 text-danger"
          />
          <div className="min-w-0 space-y-1">
            <h2
              className="text-sm font-semibold text-danger"
              id="failed-run-card-title"
            >
              任务执行未完成
            </h2>
            <p className="text-xs leading-relaxed text-text-secondary">
              {runFailureReason(run.safe_error_code)}
            </p>
          </div>
        </div>

        {canRetry ? (
          <Button
            disabled={commandPending}
            onClick={onOpenRetry}
            size="sm"
            variant="danger-outline"
          >
            <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
            重新执行
          </Button>
        ) : null}
      </div>

      {run.retry_warning?.trim() ? (
        <p
          aria-label="服务端重试警告"
          className="mt-3 rounded-lg border border-warning-border bg-warning-bg/70 px-3 py-2 text-xs leading-relaxed text-warning"
          role="note"
        >
          {run.retry_warning.trim()}
        </p>
      ) : null}
    </section>
  );
}

interface RetryRunAlertDialogProps {
  commandPending: boolean;
  onClose: () => void;
  onConfirmRetry: () => Promise<void>;
  open: boolean;
  retryError: string | null;
  retrying: boolean;
  serverRetryWarning: string | null;
}

/**
 * 温和重试确认对话框（Radix AlertDialog）：
 * 用清晰的业务中文说明重复执行可能带来的外部操作副作用，
 * 彻底隐藏 confirm_unknown=true、execution_epoch 等工程字段，确认后照常向后端透传 confirm_unknown: true。
 */
export function RetryRunAlertDialog({
  commandPending,
  onClose,
  onConfirmRetry,
  open,
  retryError,
  retrying,
  serverRetryWarning,
}: RetryRunAlertDialogProps) {
  const normalizedServerWarning = serverRetryWarning?.trim() ?? "";
  const distinctServerWarning =
    normalizedServerWarning && normalizedServerWarning !== RETRY_RISK_WARNING
      ? normalizedServerWarning
      : null;

  return (
    <AlertDialog
      onOpenChange={(nextOpen) => {
        if (!nextOpen && !commandPending) {
          onClose();
        }
      }}
      open={open}
    >
      <AlertDialogContent closeDisabled={commandPending}>
        <AlertDialogHeader>
          <AlertDialogTitle>重新执行本次任务</AlertDialogTitle>
          <AlertDialogDescription>
            系统将基于您的上一条提问从头重新处理，请确认是否继续。
          </AlertDialogDescription>
        </AlertDialogHeader>

        <AlertDialogBody className="space-y-3">
          <div className="space-y-2 rounded-lg border border-warning-border bg-warning-bg/60 p-3.5 text-xs leading-relaxed text-text-primary">
            <p
              aria-label={
                normalizedServerWarning === RETRY_RISK_WARNING
                  ? "服务端重试警告"
                  : undefined
              }
              className="font-medium text-warning"
              role={
                normalizedServerWarning === RETRY_RISK_WARNING
                  ? "note"
                  : undefined
              }
            >
              {RETRY_RISK_WARNING}
            </p>
            <ul className="list-disc space-y-1 pl-4 text-text-secondary">
              <li>此前已触发的业务写入、通知发送等外部操作可能会再次执行。</li>
              <li>若原操作结果尚未确认，建议先在目标业务系统中核对状态。</li>
              <li>系统不会自动撤销或回滚此前已产生的外部变更。</li>
            </ul>
            {distinctServerWarning ? (
              <p
                aria-label="服务端重试警告"
                className="rounded-md border border-warning-border bg-canvas/85 px-2.5 py-2 text-warning"
                role="note"
              >
                {distinctServerWarning}
              </p>
            ) : null}
          </div>

          {retryError ? (
            <InlineAlert description={retryError} variant="danger" />
          ) : null}
        </AlertDialogBody>

        <AlertDialogFooter>
          <AlertDialogCancel disabled={commandPending}>取消</AlertDialogCancel>
          {/* 阻止 Radix Action 自动关闭，保持提交态锁定并在网络失败时停留弹窗支持幂等重试。 */}
          <AlertDialogAction
            disabled={commandPending}
            loading={retrying}
            onClick={(event) => {
              event.preventDefault();
              void onConfirmRetry();
            }}
            variant="danger"
          >
            {retrying ? "正在重新执行…" : "确认重新执行"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
