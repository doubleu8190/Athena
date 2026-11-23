import {
  AlertTriangle,
  ChevronDown,
  RefreshCw,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import {
  Badge,
  Button,
  Checkbox,
  InlineAlert,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Skeleton,
  cn,
} from "../../shared/ui";
import { skillErrorMessage } from "./errorMessages";
import { usePublicSkills } from "./hooks";
import {
  skillExecutionLabel,
  skillScope,
  type Skill,
  type SkillScope,
} from "./model";
import { SkillScopeBadge } from "./SkillScopeBadge";

export interface SkillCatalogPickerProps {
  disabled?: boolean;
  disabledNote?: string;
  onChange: (skillIds: string[]) => void;
  selectedSkillIds: readonly string[];
}

const SKILL_GROUPS: readonly { scope: SkillScope; title: string }[] = [
  { scope: "company", title: "全公司技能" },
  { scope: "department", title: "部门共享技能" },
  { scope: "personal", title: "我的技能" },
];

function SkillOption({
  disabled,
  idPrefix,
  onChange,
  selected,
  skill,
}: {
  disabled: boolean;
  idPrefix: string;
  onChange: () => void;
  selected: boolean;
  skill: Skill;
}) {
  const scope = skillScope(skill);
  const checkboxId = `${idPrefix}-${skill.id}`;

  return (
    <label
      className={cn(
        "flex items-start gap-2.5 rounded-lg border px-2.5 py-2 transition-colors",
        selected
          ? "border-brand-border bg-brand-subtle/70"
          : "border-transparent hover:bg-subtle",
        disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
      )}
      htmlFor={checkboxId}
    >
      <Checkbox
        aria-label={skill.name}
        checked={selected}
        className="mt-0.5"
        disabled={disabled}
        id={checkboxId}
        onCheckedChange={onChange}
        onKeyDown={(event) => {
          // 复选框位于表单内时拦截 Enter 防止隐式提交，Space 仍保留原生勾选切换。
          if (event.key === "Enter") {
            event.preventDefault();
          }
        }}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <strong className="truncate text-xs font-semibold text-text-primary">
            {skill.name}
          </strong>
          <SkillScopeBadge compact scope={scope} />
        </div>
        <p className="mt-0.5 line-clamp-2 text-xs text-text-secondary">
          {skill.description || "未填写描述"}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-text-tertiary">
          <span>{skill.current_version_label?.trim() || "暂无版本"}</span>
          <span aria-hidden="true">·</span>
          <span>{skillExecutionLabel(skill)}</span>
        </div>
      </div>
    </label>
  );
}

/**
 * 技能挂载选择器（基于 Radix Popover + Checkbox + VisibilityBadge）：
 * - 按「全公司技能 / 部门共享技能 / 我的技能」三层范围分组并支持关键字过滤；
 * - 完整保留已选不可用技能冲突提示（绝不静默替换，仅允许用户显式移除）与恢复自动选择。
 */
export function SkillCatalogPicker({
  disabled = false,
  disabledNote = "当前不可调整，下一轮可调整。",
  onChange,
  selectedSkillIds,
}: SkillCatalogPickerProps) {
  const skills = usePublicSkills();
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState("");
  const dialogId = useId();
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const completeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (disabled && open) {
      setOpen(false);
    }
  }, [disabled, open]);

  const selected = useMemo(() => new Set(selectedSkillIds), [selectedSkillIds]);
  const catalog = useMemo(() => skills.data ?? [], [skills.data]);
  const availableIds = useMemo(
    () => new Set(catalog.map((skill) => skill.id)),
    [catalog],
  );
  const unavailableSelectedIds = useMemo(
    () =>
      skills.isSuccess
        ? selectedSkillIds.filter((skillId) => !availableIds.has(skillId))
        : [],
    [availableIds, selectedSkillIds, skills.isSuccess],
  );
  const selectedSkills = useMemo(
    () => catalog.filter((skill) => selected.has(skill.id)),
    [catalog, selected],
  );
  const visibleSelectedSkills = selectedSkills.slice(0, 1);
  const hiddenSelectedCount = Math.max(
    0,
    selectedSkillIds.length - visibleSelectedSkills.length,
  );

  const normalizedKeyword = keyword.trim().toLowerCase();
  const filteredCatalog = useMemo(() => {
    if (!normalizedKeyword) {
      return catalog;
    }
    return catalog.filter((skill) => {
      return (
        skill.name.toLowerCase().includes(normalizedKeyword) ||
        skill.description.toLowerCase().includes(normalizedKeyword) ||
        skill.slug.toLowerCase().includes(normalizedKeyword)
      );
    });
  }, [catalog, normalizedKeyword]);

  const groupedSkills = useMemo(() => {
    const groups: Record<SkillScope, Skill[]> = {
      company: [],
      department: [],
      personal: [],
    };
    for (const skill of filteredCatalog) {
      groups[skillScope(skill)].push(skill);
    }
    return groups;
  }, [filteredCatalog]);

  const closeAndReturnFocus = () => {
    setOpen(false);
    // 按钮显式关闭弹层时确保键盘焦点稳定回到触发按钮。
    queueMicrotask(() => {
      triggerRef.current?.focus();
    });
  };

  const toggle = (skillId: string) => {
    // 勾选只更新本次 Composer 的指定技能集合，不修改技能库启用状态。
    const next = new Set(selectedSkillIds);
    if (next.has(skillId)) {
      next.delete(skillId);
    } else {
      next.add(skillId);
    }
    onChange(Array.from(next));
  };

  const triggerButton = (
    <button
      aria-controls={open ? dialogId : undefined}
      aria-expanded={open}
      aria-haspopup="dialog"
      className={cn(
        "inline-flex h-8 max-w-[260px] items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
        "disabled:cursor-not-allowed disabled:opacity-50",
        unavailableSelectedIds.length > 0
          ? "border-danger-border bg-danger-bg text-danger hover:bg-danger-bg/80"
          : selectedSkillIds.length > 0
            ? "border-brand-border bg-brand-subtle text-brand hover:bg-brand-subtle/80"
            : "border-border-default bg-canvas text-text-secondary hover:border-border-strong hover:bg-subtle hover:text-text-primary",
      )}
      data-compact-control="true"
      disabled={disabled}
      ref={triggerRef}
      type="button"
    >
      <Sparkles aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
      <span className="shrink-0">本次技能</span>
      {selectedSkillIds.length === 0 ? (
        <span className="hidden shrink-0 text-text-tertiary sm:inline">
          · 自动选择
        </span>
      ) : (
        <span className="inline-flex min-w-0 items-center gap-1">
          {visibleSelectedSkills.map((skill) => (
            <span
              className="max-w-[92px] truncate rounded bg-canvas/80 px-1.5 py-0.5 text-[11px] font-medium text-brand"
              key={skill.id}
            >
              {skill.name}
            </span>
          ))}
          {hiddenSelectedCount > 0 ? (
            <Badge className="px-1.5 py-0 text-[11px]" variant="brand">
              +{hiddenSelectedCount}
            </Badge>
          ) : null}
        </span>
      )}
      {unavailableSelectedIds.length > 0 ? (
        <span className="shrink-0 rounded bg-danger/10 px-1.5 py-0.5 text-[11px] font-medium text-danger">
          含不可用项
        </span>
      ) : null}
      <ChevronDown
        aria-hidden="true"
        className="h-3.5 w-3.5 shrink-0 opacity-70"
      />
    </button>
  );

  return (
    <div
      className="inline-flex items-center"
      title={disabled && disabledNote ? disabledNote : undefined}
    >
      <Popover
        onOpenChange={(nextOpen) => {
          setOpen(nextOpen);
          if (!nextOpen) {
            setKeyword("");
          }
        }}
        open={open}
      >
        <PopoverTrigger asChild>{triggerButton}</PopoverTrigger>

        <PopoverContent
          align="start"
          aria-labelledby={titleId}
          className="w-[min(calc(100vw-24px),24rem)] p-3"
          id={dialogId}
          side="top"
        >
          <div className="flex items-start justify-between gap-2">
            <div>
              <h2
                className="text-sm font-semibold text-text-primary"
                id={titleId}
              >
                选择本次技能
              </h2>
              <p className="mt-0.5 text-xs text-text-secondary">
                手动勾选后将在本轮优先挂载；不选则由助手按任务自动判断。
              </p>
            </div>
            <button
              aria-label="关闭技能选择面板"
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-text-tertiary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={closeAndReturnFocus}
              type="button"
            >
              <X aria-hidden="true" className="h-4 w-4" />
            </button>
          </div>

          {catalog.length > 0 ? (
            <div className="relative mt-2.5">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-text-tertiary"
              />
              <input
                aria-label="搜索技能"
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
                placeholder="搜索技能名称或说明…"
                ref={searchInputRef}
                type="search"
                value={keyword}
              />
            </div>
          ) : null}

          <div className="mt-2.5">
            {skills.isPending ? (
              <div className="space-y-2 py-2" role="status">
                <p className="text-xs text-text-secondary">正在读取技能库…</p>
                <Skeleton className="h-12 w-full" />
                <Skeleton className="h-12 w-full" />
              </div>
            ) : skills.isError ? (
              <InlineAlert
                action={
                  <Button
                    onClick={() => void skills.refetch()}
                    size="xs"
                    variant="outline"
                  >
                    <RefreshCw aria-hidden="true" className="h-3 w-3" />
                    重试
                  </Button>
                }
                className="px-2.5 py-2 text-xs"
                description={skillErrorMessage(skills.error)}
                title="技能库加载失败"
                variant="danger"
              />
            ) : catalog.length === 0 ? (
              <div className="rounded-lg bg-subtle px-3 py-4 text-center text-xs text-text-secondary">
                <strong className="block font-medium text-text-primary">
                  当前没有可用技能
                </strong>
                <span className="mt-1 block">
                  管理员发布企业或部门技能，或你在“我的技能”中启用个人技能后会出现在这里；本轮仍可自动处理。
                </span>
              </div>
            ) : filteredCatalog.length === 0 ? (
              <div className="rounded-lg bg-subtle px-3 py-4 text-center text-xs text-text-secondary">
                <p>未找到匹配“{keyword.trim()}”的技能</p>
                <Button
                  className="mt-2"
                  onClick={() => {
                    // 清空后按钮会卸载，主动把焦点还回搜索输入框。
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
              <div className="max-h-64 space-y-3 overflow-y-auto pr-0.5">
                {SKILL_GROUPS.map(({ scope, title }) => {
                  const groupItems = groupedSkills[scope];
                  if (groupItems.length === 0) {
                    return null;
                  }
                  const headingId = `${dialogId}-${scope}`;
                  return (
                    <section
                      aria-labelledby={headingId}
                      className="space-y-1"
                      key={scope}
                    >
                      <h3
                        className="px-1 text-[11px] font-semibold text-text-tertiary"
                        id={headingId}
                      >
                        {title}
                      </h3>
                      <div className="space-y-1">
                        {groupItems.map((skill) => (
                          <SkillOption
                            disabled={disabled}
                            idPrefix={dialogId}
                            key={skill.id}
                            onChange={() => toggle(skill.id)}
                            selected={selected.has(skill.id)}
                            skill={skill}
                          />
                        ))}
                      </div>
                    </section>
                  );
                })}
              </div>
            )}
          </div>

          {unavailableSelectedIds.length > 0 ? (
            <div
              className="mt-3 flex items-start gap-2 rounded-lg border border-danger-border bg-danger-bg px-2.5 py-2 text-xs text-danger"
              role="alert"
            >
              <AlertTriangle
                aria-hidden="true"
                className="mt-0.5 h-4 w-4 shrink-0"
              />
              <div className="min-w-0 flex-1 space-y-1">
                <strong className="block font-semibold">
                  有已选技能不再可用
                </strong>
                <p className="leading-relaxed opacity-90">
                  技能库中找不到 {unavailableSelectedIds.length}{" "}
                  个已选项。页面不会自动替换，请重新选择或移除不可用项。
                </p>
                <button
                  className="font-medium underline underline-offset-2 hover:opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                  onClick={() => {
                    // 目录变更导致已选项失效时仅允许用户显式移除，绝不静默替换；
                    // 移除前先把焦点移至稳定的“完成”按钮，避免冲突提示卸载后焦点丢失。
                    completeButtonRef.current?.focus();
                    onChange(
                      selectedSkillIds.filter((skillId) =>
                        availableIds.has(skillId),
                      ),
                    );
                  }}
                  type="button"
                >
                  移除不可用项
                </button>
              </div>
            </div>
          ) : null}

          <footer className="mt-3 flex items-center justify-between border-t border-border-subtle pt-2.5">
            <Button
              disabled={disabled || selectedSkillIds.length === 0}
              onClick={() => {
                // 清空选择后本按钮立即禁用，先把焦点稳定移到“完成”按钮。
                completeButtonRef.current?.focus();
                onChange([]);
              }}
              size="xs"
              variant="ghost"
            >
              恢复自动选择
            </Button>
            <Button
              disabled={disabled}
              onClick={closeAndReturnFocus}
              ref={completeButtonRef}
              size="xs"
              variant="primary"
            >
              完成
            </Button>
          </footer>
        </PopoverContent>
      </Popover>

      {disabled && disabledNote ? (
        <span className="sr-only" role="status">
          {disabledNote}
        </span>
      ) : null}
    </div>
  );
}
