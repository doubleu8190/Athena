import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { getFocusableElements } from "../../shared/ui/focus";
import { Icon } from "../../shared/ui/Icons";
import { skillErrorMessage } from "./errorMessages";
import { usePublicSkills } from "./hooks";
import {
  skillExecutionLabel,
  skillScope,
  type Skill,
} from "./model";
import { SkillScopeBadge } from "./SkillScopeBadge";

interface SkillCatalogPickerProps {
  disabled?: boolean;
  disabledNote?: string;
  onChange: (skillIds: string[]) => void;
  selectedSkillIds: readonly string[];
}

function SkillOption({
  disabled,
  onChange,
  selected,
  skill,
}: {
  disabled: boolean;
  onChange: () => void;
  selected: boolean;
  skill: Skill;
}) {
  const scope = skillScope(skill);
  return (
    <label
      className={`composer-skill-option is-${scope}${
        selected ? " is-selected" : ""
      }`}
    >
      <input
        checked={selected}
        disabled={disabled}
        onChange={onChange}
        onKeyDown={(event) => {
          // 复选框位于 Composer 表单内，Enter 会触发表单隐式提交；仅拦截 Enter，Space 仍切换勾选。
          if (event.key === "Enter") {
            event.preventDefault();
          }
        }}
        type="checkbox"
      />
      <span className="composer-skill-option-main">
        <strong>{skill.name}</strong>
        <span className="composer-skill-option-description">
          {skill.description || "未填写描述"}
        </span>
        <small>
          <SkillScopeBadge compact scope={scope} />
          <span>{skill.current_version_label?.trim() || "暂无版本"}</span>
          <span aria-hidden="true">·</span>
          <span>{skillExecutionLabel(skill)}</span>
        </small>
      </span>
    </label>
  );
}

export function SkillCatalogPicker({
  disabled = false,
  disabledNote = "当前不可调整，下一轮可调整。",
  onChange,
  selectedSkillIds,
}: SkillCatalogPickerProps) {
  const skills = usePublicSkills();
  const [open, setOpen] = useState(false);
  const dialogId = useId();
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const completeButtonRef = useRef<HTMLButtonElement>(null);
  const [isMobileSheet, setIsMobileSheet] = useState(() =>
    // 与 chat.css 的移动端底部 sheet 断点保持一致，避免视觉形态与模态语义脱节。
    typeof window !== "undefined" ? window.innerWidth <= 700 : false,
  );
  const selected = new Set(selectedSkillIds);
  const catalog = skills.data ?? [];
  const availableIds = new Set(catalog.map((skill) => skill.id));
  const unavailableSelectedIds =
    skills.isSuccess
      ? selectedSkillIds.filter((skillId) => !availableIds.has(skillId))
      : [];
  const selectedSkills = catalog.filter((skill) => selected.has(skill.id));
  const visibleSelectedSkills = selectedSkills.slice(0, 2);
  const hiddenSelectedCount =
    selectedSkillIds.length - visibleSelectedSkills.length;
  const publicSkills = catalog.filter(
    (skill) => skillScope(skill) === "public",
  );
  const personalSkills = catalog.filter(
    (skill) => skillScope(skill) === "personal",
  );

  const closePicker = useCallback((returnFocus = true) => {
    setOpen(false);
    if (returnFocus) {
      triggerRef.current?.focus();
    }
  }, []);

  useEffect(() => {
    const updateViewportMode = () =>
      setIsMobileSheet(window.innerWidth <= 700);
    updateViewportMode();
    window.addEventListener("resize", updateViewportMode);
    return () => window.removeEventListener("resize", updateViewportMode);
  }, []);

  useEffect(() => {
    if (disabled && open) {
      setOpen(false);
    }
  }, [disabled, open]);

  useEffect(() => {
    if (!open) {
      return;
    }

    // 打开后先把焦点移入面板；桌面浮层保留点击外部后的外部焦点，移动端则按模态处理。
    const focusTimer = window.setTimeout(() => {
      panelRef.current?.focus();
    }, 0);
    const previousOverflow = isMobileSheet
      ? document.body.style.overflow
      : null;
    if (isMobileSheet) {
      // 移动端 sheet 打开期间锁定背景滚动，关闭或切回桌面断点时恢复原值。
      document.body.style.overflow = "hidden";
    }
    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        target instanceof Element &&
        target.closest(".composer-skill-scrim")
      ) {
        // 移动端遮罩由自身点击处理器显式关闭，避免 document 监听再次执行造成二次抢焦。
        return;
      }
      if (
        !triggerRef.current?.contains(target) &&
        !panelRef.current?.contains(target)
      ) {
        // 点击外部是用户主动移焦，不把焦点抢回触发按钮，保留外部控件当前焦点。
        closePicker(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closePicker();
        return;
      }

      if (!isMobileSheet || event.key !== "Tab" || !panelRef.current) {
        return;
      }

      // 移动端 sheet 是模态语义：Tab 与 Shift+Tab 只在弹层控件内循环，不能逃到背景页面。
      const controls = getFocusableElements(panelRef.current);
      if (controls.length === 0) {
        event.preventDefault();
        panelRef.current.focus();
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      const activeElement = document.activeElement;
      const focusIsInside = panelRef.current.contains(activeElement);
      if (event.shiftKey) {
        if (
          activeElement === first ||
          activeElement === panelRef.current ||
          !focusIsInside
        ) {
          event.preventDefault();
          last.focus();
        }
      } else if (
        activeElement === last ||
        activeElement === panelRef.current ||
        !focusIsInside
      ) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("click", closeOnOutsideClick);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("click", closeOnOutsideClick);
      document.removeEventListener("keydown", handleKeyDown, true);
      if (previousOverflow !== null) {
        document.body.style.overflow = previousOverflow;
      }
    };
  }, [closePicker, isMobileSheet, open]);

  const toggle = (skillId: string) => {
    // 勾选只更新本次 Composer 的指定技能集合，不改变技能库启用状态，也不自动写入当前对话。
    const next = new Set(selectedSkillIds);
    if (next.has(skillId)) {
      next.delete(skillId);
    } else {
      next.add(skillId);
    }
    onChange(Array.from(next));
  };

  return (
    <div className="composer-skill-picker">
      <button
        aria-controls={open ? dialogId : undefined}
        aria-expanded={open}
        aria-haspopup="dialog"
        className="composer-skill-trigger"
        disabled={disabled}
        onClick={() => {
          if (open) {
            closePicker(false);
          } else {
            setOpen(true);
          }
        }}
        ref={triggerRef}
        type="button"
      >
        <Icon name="spark" size={14} />
        <span className="composer-skill-trigger-label">本次技能</span>
        {selectedSkillIds.length === 0 ? (
          <span className="composer-skill-auto">· 自动选择</span>
        ) : (
          <>
            <span className="composer-skill-count">
              已选 {selectedSkillIds.length} 项
            </span>
            <span className="composer-skill-chips">
              {visibleSelectedSkills.map((skill) => (
                <span className="composer-skill-chip" key={skill.id}>
                  {skill.name}
                </span>
              ))}
              {hiddenSelectedCount > 0 ? (
                <span className="composer-skill-chip is-overflow">
                  +{hiddenSelectedCount}
                </span>
              ) : null}
            </span>
          </>
        )}
        {unavailableSelectedIds.length > 0 ? (
          <span className="composer-skill-conflict-indicator">
            含不可用项
          </span>
        ) : null}
        <Icon className="composer-skill-chevron" name="chevron" size={12} />
      </button>
      {disabled ? (
        <span className="composer-skill-disabled-note" role="status">
          {disabledNote}
        </span>
      ) : null}

      {open ? (
        <>
          <button
            aria-label="关闭技能选择浮层"
            className="composer-skill-scrim"
            onClick={() => {
              // 移动端遮罩是模态关闭控件，关闭时返回触发按钮；桌面外部点击仍保留外部焦点。
              closePicker(isMobileSheet);
            }}
            tabIndex={-1}
            type="button"
          />
          <div
            aria-labelledby={titleId}
            aria-modal={isMobileSheet ? "true" : undefined}
            className="composer-skill-popover"
            id={dialogId}
            ref={panelRef}
            role="dialog"
            tabIndex={-1}
          >
            <header className="composer-skill-popover-header">
              <div>
                <span className="empty-panel-kicker">Skills for this run</span>
                <h2 id={titleId}>选择本次技能</h2>
              </div>
              <button
                aria-label="关闭技能选择面板"
                className="composer-skill-close"
                onClick={() => closePicker()}
                type="button"
              >
                <Icon name="close" size={16} />
              </button>
            </header>
            <p className="composer-skill-help">
              手动选择会成为本次 Run 的指定技能；未选择时由 Agent
              按任务自动选择。指定技能不代表只使用这些技能。
            </p>

            {skills.isPending ? (
              <p className="composer-skill-state" role="status">
                正在读取技能库…
              </p>
            ) : skills.isError ? (
              <ErrorBanner
                message={skillErrorMessage(skills.error)}
                onRetry={() => void skills.refetch()}
              />
            ) : catalog.length === 0 ? (
              <div className="composer-skill-state">
                <strong>当前没有可用技能</strong>
                <span>
                  管理员发布公共技能，或你在“我的技能”中启用个人技能后，会出现在这里；本次仍可按自动选择提交。
                </span>
              </div>
            ) : (
              <div className="composer-skill-groups">
                {publicSkills.length > 0 ? (
                  <section aria-labelledby={`${dialogId}-public`}>
                    <h3 id={`${dialogId}-public`}>公共技能</h3>
                    <div className="composer-skill-options">
                      {publicSkills.map((skill) => (
                        <SkillOption
                          disabled={disabled}
                          key={skill.id}
                          onChange={() => toggle(skill.id)}
                          selected={selected.has(skill.id)}
                          skill={skill}
                        />
                      ))}
                    </div>
                  </section>
                ) : null}
                {personalSkills.length > 0 ? (
                  <section aria-labelledby={`${dialogId}-personal`}>
                    <h3 id={`${dialogId}-personal`}>我的技能</h3>
                    <div className="composer-skill-options">
                      {personalSkills.map((skill) => (
                        <SkillOption
                          disabled={disabled}
                          key={skill.id}
                          onChange={() => toggle(skill.id)}
                          selected={selected.has(skill.id)}
                          skill={skill}
                        />
                      ))}
                    </div>
                  </section>
                ) : null}
              </div>
            )}

            {unavailableSelectedIds.length > 0 ? (
              <div className="composer-skill-conflict" role="alert">
                <Icon name="shield" size={16} />
                <div>
                  <strong>有已选技能不再可用</strong>
                  <p>
                    技能库中找不到 {unavailableSelectedIds.length}{" "}
                    个已选项。页面不会自动替换，请重新选择或移除不可用项。
                  </p>
                  <button
                    className="text-button"
                    onClick={() => {
                      // 目录刷新导致已选项失效时只允许用户显式清理，不能偷偷替换成其他技能。
                      // 先移到稳定的完成按钮，避免移除后整块冲突提示消失导致焦点落到 body。
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

            <footer className="composer-skill-footer">
              <button
                className="button-quiet"
                disabled={disabled || selectedSkillIds.length === 0}
                onClick={() => {
                  // 恢复自动选择后本按钮会立即禁用，先把焦点移到稳定的完成按钮，避免焦点落到 body。
                  completeButtonRef.current?.focus();
                  onChange([]);
                }}
                type="button"
              >
                恢复自动选择
              </button>
              <button
                className="button-primary"
                disabled={disabled}
                onClick={() => closePicker()}
                ref={completeButtonRef}
                type="button"
              >
                完成
              </button>
            </footer>
          </div>
        </>
      ) : null}
    </div>
  );
}
