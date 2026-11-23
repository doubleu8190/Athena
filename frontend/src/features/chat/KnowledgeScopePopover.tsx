import {
  BookOpen,
  ChevronDown,
  RefreshCw,
  Search,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import {
  Button,
  Checkbox,
  InlineAlert,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Skeleton,
  Switch,
  VisibilityBadge,
  cn,
  type VisibilityScope,
} from "../../shared/ui";
import { knowledgeErrorMessage } from "../knowledge/errorMessages";
import { useKnowledgeDirectory } from "../knowledge/hooks";
import {
  PROJECT_KNOWLEDGE_LIMIT,
  knowledgeBaseScope,
  type KnowledgeBase,
} from "../knowledge/model";

const MAX_SELECTED_KNOWLEDGE_BASES = PROJECT_KNOWLEDGE_LIMIT;

const SCOPE_GROUPS: readonly { scope: VisibilityScope; title: string }[] = [
  { scope: "company", title: "全公司" },
  { scope: "department", title: "部门共享" },
  { scope: "personal", title: "个人" },
];

export interface KnowledgeScopePopoverProps {
  disabled?: boolean;
  /** 是否处于项目上下文中（用于默认态附带含当前项目库的说明）。 */
  hasProjectContext?: boolean;
  onChange: (knowledgeBaseIds: string[] | null) => void;
  value: string[] | null;
}

/**
 * 知识库三态范围选择器：
 * - value === null：企业默认态（自动连接全公司 + 本部门知识库；项目内含项目关联库）
 * - value === []：显式关闭态（本轮不检索知识库）
 * - value === string[]（非空）：显式指定态（最多 20 个，按三层可见性分组勾选）
 */
export function KnowledgeScopePopover({
  disabled = false,
  hasProjectContext = false,
  onChange,
  value,
}: KnowledgeScopePopoverProps) {
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState("");
  const titleId = useId();
  const switchId = useId();
  const searchInputRef = useRef<HTMLInputElement>(null);
  const completeButtonRef = useRef<HTMLButtonElement>(null);

  // 仅在浮层展开时按需拉取知识库目录，减少首页冷启动请求并充分利用 React Query 缓存。
  const directory = useKnowledgeDirectory(open);

  useEffect(() => {
    if (disabled && open) {
      setOpen(false);
    }
  }, [disabled, open]);

  const isOff = Array.isArray(value) && value.length === 0;
  const isExplicit = Array.isArray(value) && value.length > 0;
  const isEnabled = !isOff;
  const selectedIds = useMemo(
    () => (Array.isArray(value) ? value : []),
    [value],
  );
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const isAtLimit = selectedIds.length >= MAX_SELECTED_KNOWLEDGE_BASES;

  const triggerLabel = isOff
    ? "不使用知识库"
    : isExplicit
      ? `已选 ${selectedIds.length} 个知识库`
      : "企业与部门知识库";

  const normalizedKeyword = keyword.trim().toLowerCase();
  const allBases = useMemo(() => directory.data ?? [], [directory.data]);
  const filteredBases = useMemo(() => {
    if (!normalizedKeyword) {
      return allBases;
    }
    return allBases.filter((base) => {
      const nameMatch = base.name.toLowerCase().includes(normalizedKeyword);
      const descMatch = (base.description ?? "")
        .toLowerCase()
        .includes(normalizedKeyword);
      return nameMatch || descMatch;
    });
  }, [allBases, normalizedKeyword]);

  const groupedBases = useMemo(() => {
    const groups: Record<VisibilityScope, KnowledgeBase[]> = {
      company: [],
      department: [],
      personal: [],
    };
    for (const base of filteredBases) {
      groups[knowledgeBaseScope(base)].push(base);
    }
    return groups;
  }, [filteredBases]);

  const handleToggleRetrieval = (checked: boolean) => {
    // 关闭检索时显式写入空数组 []；重新开启时恢复为企业默认态 null。
    onChange(checked ? null : []);
  };

  const handleToggleBase = (baseId: string) => {
    if (disabled) {
      return;
    }
    if (selectedSet.has(baseId)) {
      const next = selectedIds.filter((id) => id !== baseId);
      // 取消最后一个显式勾选的知识库时平滑回到默认态 null，避免在开关仍开启时误切为关闭态 []。
      onChange(next.length === 0 ? null : next);
      return;
    }
    if (selectedIds.length >= MAX_SELECTED_KNOWLEDGE_BASES) {
      return;
    }
    onChange([...selectedIds, baseId]);
  };

  return (
    <Popover
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) {
          setKeyword("");
        }
      }}
      open={open}
    >
      <PopoverTrigger asChild>
        <button
          aria-label={`知识库范围：${triggerLabel}`}
          className={cn(
            "inline-flex h-8 max-w-[220px] items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors",
            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
            "disabled:cursor-not-allowed disabled:opacity-50",
            isOff
              ? "border-border-default bg-subtle text-text-secondary hover:border-border-strong hover:bg-muted"
              : "border-brand-border bg-brand-subtle text-brand hover:bg-brand-subtle/80",
          )}
          data-compact-control="true"
          data-knowledge-mode={isOff ? "off" : isExplicit ? "explicit" : "default"}
          disabled={disabled}
          type="button"
        >
          <BookOpen aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{triggerLabel}</span>
          {!isOff && !isExplicit && hasProjectContext ? (
            <span className="hidden shrink-0 text-[11px] opacity-80 sm:inline">
              · 含项目库
            </span>
          ) : null}
          <ChevronDown
            aria-hidden="true"
            className="h-3.5 w-3.5 shrink-0 opacity-70"
          />
        </button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        aria-labelledby={titleId}
        className="w-80 p-3 sm:w-96"
        side="top"
      >
        <div className="flex items-start justify-between gap-3 border-b border-border-subtle pb-3">
          <div className="min-w-0">
            <label
              className="block cursor-pointer text-sm font-semibold text-text-primary"
              htmlFor={switchId}
              id={titleId}
            >
              启用知识库检索
            </label>
            <p className="mt-0.5 text-xs text-text-secondary">
              {isOff
                ? "已关闭，本轮对话不会检索任何知识库。"
                : isExplicit
                  ? `已指定 ${selectedIds.length} / ${MAX_SELECTED_KNOWLEDGE_BASES} 个知识库。`
                  : hasProjectContext
                    ? "默认检索全公司、本部门及当前项目知识库。"
                    : "默认自动检索全公司与本部门共享知识库。"}
            </p>
          </div>
          <Switch
            checked={isEnabled}
            disabled={disabled}
            id={switchId}
            onCheckedChange={handleToggleRetrieval}
            size="sm"
          />
        </div>

        {isEnabled ? (
          <div className="mt-3 space-y-2.5">
            <div className="relative">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-text-tertiary"
              />
              <input
                aria-label="搜索知识库"
                className={cn(
                  "h-8 w-full rounded-lg border border-border-default bg-canvas pr-3 pl-8 text-xs text-text-primary",
                  "placeholder:text-text-placeholder focus-visible:border-ring-focus focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/20",
                )}
                onChange={(event) => setKeyword(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                  }
                }}
                placeholder="搜索知识库名称或说明…"
                ref={searchInputRef}
                type="search"
                value={keyword}
              />
            </div>

            {isAtLimit ? (
              <InlineAlert
                className="px-2.5 py-2 text-xs"
                description={`单轮对话最多指定 ${MAX_SELECTED_KNOWLEDGE_BASES} 个知识库，如需更换请先取消已选项。`}
                variant="warning"
              />
            ) : null}

            {directory.isPending ? (
              <div aria-label="正在加载知识库列表" className="space-y-2 py-1">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            ) : directory.isError ? (
              <InlineAlert
                action={
                  <Button
                    onClick={() => void directory.refetch()}
                    size="xs"
                    variant="outline"
                  >
                    <RefreshCw aria-hidden="true" className="h-3 w-3" />
                    重试
                  </Button>
                }
                className="px-2.5 py-2 text-xs"
                description={knowledgeErrorMessage(directory.error)}
                title="知识库目录加载失败"
                variant="danger"
              />
            ) : allBases.length === 0 ? (
              <p className="rounded-lg bg-subtle px-3 py-4 text-center text-xs text-text-secondary">
                当前暂无可手动勾选的知识库，默认模式仍将自动连接您的授权范围。
              </p>
            ) : filteredBases.length === 0 ? (
              <div className="rounded-lg bg-subtle px-3 py-4 text-center text-xs text-text-secondary">
                <p>未找到匹配“{keyword.trim()}”的知识库</p>
                <Button
                  className="mt-2"
                  onClick={() => {
                    // 清空后按钮会卸载，主动将焦点归还搜索框避免落回 body。
                    setKeyword("");
                    searchInputRef.current?.focus();
                  }}
                  size="xs"
                  variant="ghost"
                >
                  清空搜索
                </Button>
              </div>
            ) : (
              <div className="max-h-60 space-y-3 overflow-y-auto pr-0.5">
                {SCOPE_GROUPS.map(({ scope, title }) => {
                  const groupItems = groupedBases[scope];
                  if (groupItems.length === 0) {
                    return null;
                  }
                  return (
                    <section
                      aria-label={`${title}知识库`}
                      className="space-y-1"
                      key={scope}
                    >
                      <h3 className="px-1 text-[11px] font-semibold text-text-tertiary">
                        {title}（{groupItems.length}）
                      </h3>
                      <div className="space-y-1">
                        {groupItems.map((base) => {
                          const checked = selectedSet.has(base.id);
                          const itemDisabled =
                            disabled || (!checked && isAtLimit);
                          const checkboxId = `${switchId}-kb-${base.id}`;
                          return (
                            <label
                              className={cn(
                                "flex items-start gap-2.5 rounded-lg border px-2.5 py-2 transition-colors",
                                checked
                                  ? "border-brand-border bg-brand-subtle/70"
                                  : "border-transparent hover:bg-subtle",
                                itemDisabled
                                  ? "cursor-not-allowed opacity-60"
                                  : "cursor-pointer",
                              )}
                              htmlFor={checkboxId}
                              key={base.id}
                            >
                              <Checkbox
                                aria-label={base.name}
                                checked={checked}
                                className="mt-0.5"
                                disabled={itemDisabled}
                                id={checkboxId}
                                onCheckedChange={() =>
                                  handleToggleBase(base.id)
                                }
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    event.preventDefault();
                                  }
                                }}
                              />
                              <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-1.5">
                                  <span className="truncate text-xs font-medium text-text-primary">
                                    {base.name}
                                  </span>
                                  <VisibilityBadge
                                    className="px-1.5 py-0 text-[11px]"
                                    scope={scope}
                                  />
                                </div>
                                {base.description ? (
                                  <p className="mt-0.5 line-clamp-1 text-[11px] text-text-tertiary">
                                    {base.description}
                                  </p>
                                ) : null}
                              </div>
                            </label>
                          );
                        })}
                      </div>
                    </section>
                  );
                })}
              </div>
            )}

            <div className="flex items-center justify-between border-t border-border-subtle pt-2.5">
              <Button
                disabled={disabled || value === null}
                onClick={() => {
                  // 恢复默认后本按钮会立即禁用，先把焦点移至稳定的“完成”按钮避免落回 body。
                  completeButtonRef.current?.focus();
                  onChange(null);
                }}
                size="xs"
                variant="ghost"
              >
                恢复默认范围
              </Button>
              <Button
                onClick={() => setOpen(false)}
                ref={completeButtonRef}
                size="xs"
                variant="primary"
              >
                完成
              </Button>
            </div>
          </div>
        ) : (
          <div className="mt-3 flex items-center justify-between pt-1">
            <Button
              disabled={disabled}
              onClick={() => {
                // 切回默认态后会重新挂载列表区，先把焦点交由稳定按钮保持键盘可达。
                completeButtonRef.current?.focus();
                onChange(null);
              }}
              size="xs"
              variant="ghost"
            >
              恢复默认范围
            </Button>
            <Button
              onClick={() => setOpen(false)}
              ref={completeButtonRef}
              size="xs"
              variant="primary"
            >
              完成
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
