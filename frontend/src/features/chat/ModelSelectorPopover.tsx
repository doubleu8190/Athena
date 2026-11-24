import {
  AlertTriangle,
  Check,
  ChevronDown,
  Cpu,
  Eye,
  RefreshCw,
} from "lucide-react";
import { useEffect, useId, useState } from "react";

import {
  Badge,
  Button,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Skeleton,
  Tooltip,
  cn,
  toast,
} from "../../shared/ui";
import { useUserModels } from "../models/hooks";
import {
  resolveEffectiveUserModel,
  userModelDisplayLabel,
} from "../models/model";

export interface ModelSelectorPopoverProps {
  disabled?: boolean;
  /** 当前输入区已附加图片文件时，若生效模型不支持视觉则展示温和琥珀提醒。 */
  hasImageAttachment?: boolean;
  onChange: (modelProfileId: string | null) => void;
  value: string | null;
}

/**
 * 场景化模型选择器（GET /api/v1/models）：
 * - 目录请求失败或为空时优雅降级显示「默认模型」，绝不阻塞消息发送；
 * - 检测到图片附件且当前生效模型 supports_vision === false 时展示温和视觉提醒。
 */
export function ModelSelectorPopover({
  disabled = false,
  hasImageAttachment = false,
  onChange,
  value,
}: ModelSelectorPopoverProps) {
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const modelsQuery = useUserModels();

  useEffect(() => {
    if (disabled && open) {
      setOpen(false);
    }
  }, [disabled, open]);

  const catalog = modelsQuery.data;
  const items = catalog?.items ?? [];
  const isSelectedModelUnavailable = Boolean(
    modelsQuery.isSuccess &&
      value !== null &&
      !items.some((item) => item.id === value),
  );
  const effectiveModel = isSelectedModelUnavailable
    ? null
    : resolveEffectiveUserModel(catalog, value);
  const triggerLabel = isSelectedModelUnavailable
    ? "已选模型不可用"
    : effectiveModel
      ? userModelDisplayLabel(effectiveModel)
      : "默认模型";
  // 仅在明确得知当前生效模型不支持识图且存在图片附件时展示提醒，避免目录加载中或模型失效态误报。
  const showVisionWarning = Boolean(
    hasImageAttachment && effectiveModel && !effectiveModel.supports_vision,
  );
  const recommendedVisionModel =
    items.find((item) => item.supports_vision) ?? null;
  const recommendedVisionLabel = recommendedVisionModel
    ? userModelDisplayLabel(recommendedVisionModel)
    : null;

  return (
    <div className="inline-flex items-center gap-1">
      <Popover onOpenChange={setOpen} open={open}>
        <PopoverTrigger asChild>
          <button
            aria-label={`选择模型：${triggerLabel}`}
            className={cn(
              "inline-flex h-8 max-w-[180px] items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
              "disabled:cursor-not-allowed disabled:opacity-50",
              isSelectedModelUnavailable
                ? "border-warning-border bg-warning-bg text-warning hover:bg-warning-bg/80"
                : value !== null
                  ? "border-brand-border bg-brand-subtle text-brand hover:bg-brand-subtle/80"
                  : "border-border-default bg-canvas text-text-secondary hover:border-border-strong hover:bg-subtle hover:text-text-primary",
            )}
            data-compact-control="true"
            disabled={disabled}
            type="button"
          >
            <Cpu aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">{triggerLabel}</span>
            <ChevronDown
              aria-hidden="true"
              className="h-3.5 w-3.5 shrink-0 opacity-70"
            />
          </button>
        </PopoverTrigger>

        <PopoverContent
          align="start"
          aria-labelledby={titleId}
          className="w-80 p-2"
          side="top"
        >
          <div className="flex items-center justify-between px-2 py-1.5">
            <div>
              <h2
                className="text-xs font-semibold text-text-primary"
                id={titleId}
              >
                选择对话模型
              </h2>
              <p className="mt-0.5 text-[11px] text-text-tertiary">
                按办公场景切换最合适的模型
              </p>
            </div>
          </div>

          {isSelectedModelUnavailable ? (
            <div
              className="mx-1 mb-2 space-y-2 rounded-lg border border-warning-border bg-warning-bg px-2.5 py-2 text-xs text-warning"
              role="alert"
            >
              <div className="flex items-start gap-2">
                <AlertTriangle
                  aria-hidden="true"
                  className="mt-0.5 h-3.5 w-3.5 shrink-0"
                />
                <span>当前已选模型已不可用，请切换其他模型或恢复默认模型。</span>
              </div>
              <Button
                onClick={() => {
                  onChange(null);
                  setOpen(false);
                }}
                size="xs"
                variant="outline"
              >
                恢复默认模型
              </Button>
            </div>
          ) : null}

          {showVisionWarning ? (
            <div
              className="mx-1 mb-2 space-y-2 rounded-lg border border-warning-border bg-warning-bg px-2.5 py-2 text-xs text-warning"
              role="status"
            >
              <div className="flex items-start gap-2">
                <AlertTriangle
                  aria-hidden="true"
                  className="mt-0.5 h-3.5 w-3.5 shrink-0"
                />
                <span>
                  {recommendedVisionModel
                    ? "当前模型暂不支持识图，仍可直接发送，或切换至识图模型。"
                    : "当前模型暂不支持识图，仍可直接发送；当前暂无可用识图模型。"}
                </span>
              </div>
              {recommendedVisionModel && recommendedVisionLabel ? (
                <Button
                  disabled={disabled}
                  onClick={() => {
                    onChange(recommendedVisionModel.id);
                    toast.success(`已切换至识图模型「${recommendedVisionLabel}」`);
                    setOpen(false);
                  }}
                  size="xs"
                  variant="outline"
                >
                  <Eye aria-hidden="true" className="h-3 w-3" />
                  一键切至「{recommendedVisionLabel}」
                </Button>
              ) : null}
            </div>
          ) : null}

          {modelsQuery.isPending ? (
            <div aria-label="正在加载模型列表" className="space-y-1.5 p-2">
              <Skeleton className="h-11 w-full" />
              <Skeleton className="h-11 w-full" />
            </div>
          ) : modelsQuery.isError ? (
            <div className="space-y-2 rounded-lg border border-border-subtle bg-subtle p-3 text-xs text-text-secondary">
              <p>模型目录加载失败，发送时将自动使用系统默认模型。</p>
              <Button
                onClick={() => void modelsQuery.refetch()}
                size="xs"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" className="h-3 w-3" />
                重新加载
              </Button>
            </div>
          ) : items.length === 0 ? (
            <div className="rounded-lg bg-subtle p-3 text-xs text-text-secondary">
              暂无可切换的专用模型，当前将使用系统默认模型。
            </div>
          ) : (
            <div
              aria-label="可用模型列表"
              className="max-h-72 space-y-1 overflow-y-auto"
              role="listbox"
            >
              {items.map((model) => {
                const isDefault =
                  model.is_default || model.id === catalog?.default_model_id;
                const isSelected =
                  value !== null
                    ? model.id === value
                    : model.id === effectiveModel?.id;
                const label = userModelDisplayLabel(model);

                return (
                  <button
                    aria-selected={isSelected}
                    className={cn(
                      "flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors",
                      "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                      isSelected
                        ? "bg-brand-subtle text-brand"
                        : "text-text-primary hover:bg-subtle",
                    )}
                    key={model.id}
                    onClick={() => {
                      onChange(model.id);
                      setOpen(false);
                    }}
                    role="option"
                    type="button"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="truncate text-sm font-medium">
                          {label}
                        </span>
                        {isDefault ? (
                          <Badge className="px-1.5 py-0 text-[11px]" variant="brand">
                            默认
                          </Badge>
                        ) : null}
                        {model.supports_vision ? (
                          <Badge className="px-1.5 py-0 text-[11px]" variant="info">
                            <Eye aria-hidden="true" className="h-3 w-3" />
                            识图
                          </Badge>
                        ) : null}
                      </div>
                      {model.scenario_description ? (
                        <p className="mt-0.5 line-clamp-2 text-xs text-text-secondary">
                          {model.scenario_description}
                        </p>
                      ) : null}
                    </div>
                    <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center">
                      {isSelected ? (
                        <Check
                          aria-hidden="true"
                          className="h-4 w-4 text-brand"
                        />
                      ) : null}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </PopoverContent>
      </Popover>

      {showVisionWarning ? (
        <Tooltip content="当前模型不支持识图，建议切换支持视觉的模型">
          <span
            aria-label="当前模型不支持识图，建议切换支持视觉的模型"
            className="inline-flex h-5 w-5 shrink-0 cursor-help items-center justify-center rounded-full border border-warning-border bg-warning-bg text-warning focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            data-testid="model-vision-warning"
            role="status"
            tabIndex={0}
          >
            <span
              aria-hidden="true"
              className="h-2 w-2 rounded-full bg-amber-500"
            />
          </span>
        </Tooltip>
      ) : null}
    </div>
  );
}
