import { useEffect, useId, useRef, useState } from "react";

import { getAuthIdentityEpoch } from "../../shared/api/client";
import {
  ADMIN_MUTED_CLASS,
  Badge,
  Button,
  CHECKBOX_INPUT_CLASS,
  CHECKBOX_LABEL_CLASS,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_SELECT_CLASS,
  FIELD_TEXTAREA_CLASS,
  FORM_GRID_2_CLASS,
  FORM_STACK_CLASS,
  MODAL_WIDTH_XL,
  PAGE_LOADING_CLASS,
  cn,
} from "../../shared/ui";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { FieldError } from "../../shared/ui/FieldError";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import {
  adminErrorMessage,
  isSubagentSlugConflict,
  isSubagentVersionConflict,
} from "./errorMessages";
import {
  useCreateSubagentMutation,
  useModelProfiles,
  useSubagent,
  useSubagentAvailableTools,
  useUpdateSubagentMutation,
} from "./hooks";
import {
  MAX_SUBAGENT_ALLOWED_TOOLS,
  MAX_SUBAGENT_DESCRIPTION_LENGTH,
  MAX_SUBAGENT_NAME_LENGTH,
  MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH,
  subagentForm,
  validateSubagentForm,
  type FieldErrors,
  type SubagentField,
  type SubagentFormValues,
} from "./model";

interface SubagentDialogProps {
  onClose: () => void;
  open: boolean;
  subagentId?: string;
}

export function SubagentDialog({
  onClose,
  open,
  subagentId,
}: SubagentDialogProps) {
  const formId = useId();
  const editing = Boolean(subagentId);
  const detail = useSubagent(open && editing ? (subagentId ?? null) : null);
  // 仅在弹窗打开时按需拉取模型与可选工具目录，避免进入管理页未打开弹窗即触发全量请求。
  const models = useModelProfiles(open);
  const availableTools = useSubagentAvailableTools(open);
  const createMutation = useCreateSubagentMutation();
  const updateMutation = useUpdateSubagentMutation(subagentId ?? "");
  const { reset: resetCreateMutation } = createMutation;
  const { reset: resetUpdateMutation } = updateMutation;

  const [values, setValues] = useState<SubagentFormValues>(() =>
    subagentForm(),
  );
  const [errors, setErrors] = useState<FieldErrors<SubagentField>>({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const [versionConflict, setVersionConflict] = useState(false);
  const [loadedVersionKey, setLoadedVersionKey] = useState<string | null>(null);
  const submitLock = useRef(false);

  const pending = createMutation.isPending || updateMutation.isPending;

  useEffect(() => {
    if (!open) {
      setLoadedVersionKey(null);
      submitLock.current = false;
      return;
    }
    setErrors({});
    setRequestError(null);
    setVersionConflict(false);
    resetCreateMutation();
    resetUpdateMutation();
    submitLock.current = false;
    if (!editing) {
      setValues(subagentForm());
    }
  }, [editing, open, resetCreateMutation, resetUpdateMutation]);

  useEffect(() => {
    if (!open || !editing || !detail.data) {
      return;
    }
    // 使用 id + version 作为加载键，版本刷新后才同步覆盖表单，避免输入被意外重置。
    const nextKey = `${detail.data.id}:${detail.data.version}`;
    if (loadedVersionKey !== nextKey) {
      setValues(subagentForm(detail.data));
      setLoadedVersionKey(nextKey);
      setVersionConflict(false);
      setRequestError(null);
    }
  }, [detail.data, editing, loadedVersionKey, open]);

  const toggleTool = (toolId: string) => {
    setValues((current) => {
      const exists = current.allowed_tool_ids.includes(toolId);
      if (
        !exists &&
        current.allowed_tool_ids.length >= MAX_SUBAGENT_ALLOWED_TOOLS
      ) {
        return current;
      }
      return {
        ...current,
        allowed_tool_ids: exists
          ? current.allowed_tool_ids.filter((id) => id !== toolId)
          : [...current.allowed_tool_ids, toolId],
      };
    });
  };

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitLock.current || pending) {
      return;
    }
    const { errors: validationErrors, createInput } =
      validateSubagentForm(values);
    setErrors(validationErrors);
    setRequestError(null);
    setVersionConflict(false);
    if (!createInput) {
      return;
    }

    if (editing && !detail.data) {
      return;
    }

    submitLock.current = true;
    const identityEpoch = getAuthIdentityEpoch();
    try {
      if (editing && detail.data) {
        await updateMutation.mutateAsync({
          identityEpoch,
          input: {
            version: detail.data.version,
            slug: createInput.slug,
            name: createInput.name,
            description: createInput.description,
            system_prompt: createInput.system_prompt,
            model_profile_id: createInput.model_profile_id,
            allowed_tool_ids: createInput.allowed_tool_ids,
            max_llm_requests: createInput.max_llm_requests,
            timeout_seconds: createInput.timeout_seconds,
          },
        });
      } else {
        await createMutation.mutateAsync({
          identityEpoch,
          input: createInput,
        });
      }
      onClose();
    } catch (error) {
      const message = adminErrorMessage(error);
      if (isSubagentSlugConflict(error)) {
        setErrors((prev) => ({ ...prev, slug: message }));
      }
      if (isSubagentVersionConflict(error)) {
        setVersionConflict(true);
      }
      setRequestError(message);
    } finally {
      submitLock.current = false;
    }
  };

  const modelOptions = models.data?.items ?? [];
  const defaultModelId = models.data?.default_profile_id ?? null;
  const selectedToolCount = new Set(values.allowed_tool_ids).size;
  const toolLimitReached =
    selectedToolCount >= MAX_SUBAGENT_ALLOWED_TOOLS;

  return (
    <Modal
      closeDisabled={pending}
      description={
        editing
          ? "修改子智能体提示词、模型绑定与工具白名单；提交时校验乐观锁版本号。"
          : "配置可被主智能体按需调度的子智能体，并授予最小必要工具权限。"
      }
      footer={
        <>
          <Button
            disabled={pending}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={pending || (editing && detail.isPending)}
            form={formId}
            type="submit"
            variant="primary"
          >
            {pending
              ? "正在保存…"
              : editing
                ? "保存修改"
                : "创建子智能体"}
          </Button>
        </>
      }
      onClose={() => {
        if (!pending) {
          onClose();
        }
      }}
      open={open}
      panelClassName={MODAL_WIDTH_XL}
      title={editing ? "编辑子智能体" : "新建子智能体"}
    >
      {editing && detail.isPending ? (
        <div className={PAGE_LOADING_CLASS} role="status">
          正在读取子智能体配置…
        </div>
      ) : editing && detail.isError ? (
        <ErrorBanner
          message={adminErrorMessage(detail.error)}
          onRetry={() => void detail.refetch()}
        />
      ) : (
        <form
          className={FORM_STACK_CLASS}
          id={formId}
          noValidate
          onSubmit={(event) => void handleSubmit(event)}
        >
          {requestError ? (
            <ErrorBanner
              message={requestError}
              onRetry={
                versionConflict
                  ? () => {
                      setRequestError(null);
                      setVersionConflict(false);
                      setLoadedVersionKey(null);
                      void detail.refetch();
                    }
                  : undefined
              }
              retryLabel={versionConflict ? "重新加载最新配置" : undefined}
            />
          ) : null}

          <div className={FORM_GRID_2_CLASS}>
            <div className={FIELD_CLASS}>
              <label htmlFor={`${formId}-slug`}>Slug 标识</label>
              <input
                aria-describedby={`${formId}-slug-hint ${formId}-slug-error`}
                aria-invalid={Boolean(errors.slug)}
                className={FIELD_INPUT_CLASS}
                disabled={pending}
                id={`${formId}-slug`}
                onChange={(event) =>
                  setValues((current) => ({
                    ...current,
                    slug: event.target.value,
                  }))
                }
                placeholder="例如：research_assistant"
                type="text"
                value={values.slug}
              />
              <small id={`${formId}-slug-hint`}>
                仅支持小写字母、数字、连字符和下划线（3–64 位），不可使用保留字：
                main、default、system、subagent。
              </small>
              <FieldError error={errors.slug} id={`${formId}-slug-error`} />
            </div>

            <div className={FIELD_CLASS}>
              <label htmlFor={`${formId}-name`}>显示名称</label>
              <input
                aria-describedby={`${formId}-name-hint ${formId}-name-error`}
                aria-invalid={Boolean(errors.name)}
                className={FIELD_INPUT_CLASS}
                disabled={pending}
                id={`${formId}-name`}
                onChange={(event) =>
                  setValues((current) => ({
                    ...current,
                    name: event.target.value,
                  }))
                }
                placeholder="例如：调研助手"
                type="text"
                value={values.name}
              />
              <small id={`${formId}-name-hint`}>
                1–{MAX_SUBAGENT_NAME_LENGTH} 个 Unicode
                字符；不能包含 C0 控制字符（U+0000–U+001F）或 DEL（U+007F）。
              </small>
              <FieldError error={errors.name} id={`${formId}-name-error`} />
            </div>
          </div>

          <div className={FIELD_CLASS}>
            <label htmlFor={`${formId}-description`}>能力描述</label>
            <textarea
              aria-describedby={`${formId}-description-hint ${formId}-description-error`}
              aria-invalid={Boolean(errors.description)}
              className={FIELD_TEXTAREA_CLASS}
              disabled={pending}
              id={`${formId}-description`}
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  description: event.target.value,
                }))
              }
              placeholder="简述该子智能体擅长的任务范围，主智能体将据此决定是否调度。"
              rows={3}
              value={values.description}
            />
            <small id={`${formId}-description-hint`}>
              1–{MAX_SUBAGENT_DESCRIPTION_LENGTH.toLocaleString()} 个 Unicode
              字符；允许换行、回车和制表符，不允许其他 C0 控制字符或
              DEL（U+007F）；C1（U+0080–U+009F）按后端规则允许。
            </small>
            <FieldError
              error={errors.description}
              id={`${formId}-description-error`}
            />
          </div>

          <div className={FIELD_CLASS}>
            <label htmlFor={`${formId}-model`}>绑定模型</label>
            <select
              className={FIELD_SELECT_CLASS}
              disabled={pending || models.isPending}
              id={`${formId}-model`}
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  model_profile_id: event.target.value,
                }))
              }
              value={values.model_profile_id}
            >
              <option value="">跟随系统默认</option>
              {modelOptions.map((model) => (
                <option
                  disabled={!model.enabled}
                  key={model.id}
                  value={model.id}
                >
                  {model.name} · {model.model}
                  {model.id === defaultModelId ? "（系统默认）" : ""}
                  {!model.enabled ? "（已停用）" : ""}
                </option>
              ))}
            </select>
          </div>

          <div className={FIELD_CLASS}>
            <label htmlFor={`${formId}-prompt`}>System Prompt</label>
            <textarea
              aria-describedby={`${formId}-prompt-hint ${formId}-prompt-error`}
              aria-invalid={Boolean(errors.system_prompt)}
              className={FIELD_TEXTAREA_CLASS}
              disabled={pending}
              id={`${formId}-prompt`}
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  system_prompt: event.target.value,
                }))
              }
              placeholder="定义子智能体的角色设定、执行边界与输出规范。"
              rows={5}
              value={values.system_prompt}
            />
            <small id={`${formId}-prompt-hint`}>
              1–{MAX_SUBAGENT_SYSTEM_PROMPT_LENGTH.toLocaleString()} 个 Unicode
              字符；允许换行、回车和制表符，不允许其他 C0 控制字符或
              DEL（U+007F）；C1（U+0080–U+009F）按后端规则允许。
            </small>
            <FieldError
              error={errors.system_prompt}
              id={`${formId}-prompt-error`}
            />
          </div>

          <fieldset
            aria-describedby={`${formId}-tools-hint ${formId}-tools-count ${formId}-tools-error`}
            aria-invalid={Boolean(errors.allowed_tool_ids)}
            className="m-0 flex min-w-0 flex-col gap-2.5 rounded-[10px] border border-border-default bg-subtle p-[13px]"
          >
            <legend className="px-1.5 text-[13px] font-semibold text-text-primary">
              工具权限白名单
            </legend>
            <p
              className="text-xs leading-[1.65] text-text-tertiary [overflow-wrap:anywhere] [&>svg]:mr-1 [&>svg]:inline-block [&>svg]:align-[-2px]"
              id={`${formId}-tools-hint`}
            >
              <Icon name="shield" size={13} />{" "}
              最多可选择 {MAX_SUBAGENT_ALLOWED_TOOLS} 项。
              系统已自动禁止子智能体嵌套调度（<code>dispatch_subagents</code>{" "}
              不会出现在可选目录中）。
            </p>
            <p
              aria-live="polite"
              className={cn(
                "text-xs leading-[1.65] [overflow-wrap:anywhere]",
                toolLimitReached
                  ? "font-semibold text-brand-hover"
                  : "text-text-tertiary",
              )}
              id={`${formId}-tools-count`}
              role="status"
            >
              已选择 {selectedToolCount} / {MAX_SUBAGENT_ALLOWED_TOOLS} 项
              {toolLimitReached
                ? "，已达到上限；取消已选工具后才能继续选择。"
                : "。"}
            </p>
            {availableTools.isPending ? (
              <p className={ADMIN_MUTED_CLASS} role="status">
                正在读取可选工具目录…
              </p>
            ) : availableTools.isError ? (
              <ErrorBanner
                message={adminErrorMessage(availableTools.error)}
                onRetry={() => void availableTools.refetch()}
              />
            ) : availableTools.data.groups.length === 0 ? (
              <p className={ADMIN_MUTED_CLASS}>当前没有可授予的工具。</p>
            ) : (
              <div className="flex max-h-[min(42svh,320px)] min-w-0 flex-col gap-3 overflow-x-hidden overflow-y-auto pr-0.5 min-[721px]:max-h-[280px]">
                {availableTools.data.groups.map((group) => (
                  <div
                    className="flex min-w-0 flex-col gap-1.5"
                    key={group.group_key}
                  >
                    <div className="flex min-w-0 items-center justify-between gap-2.5 border-b border-border-subtle pb-[5px] text-xs text-text-secondary">
                      <strong>{group.label}</strong>
                      <span>{group.items.length} 项</span>
                    </div>
                    <ul className="m-0 flex min-w-0 list-none flex-col gap-1.5 p-0">
                      {group.items.map((tool) => {
                        const checked = values.allowed_tool_ids.includes(
                          tool.tool_id,
                        );
                        return (
                          <li key={tool.tool_id}>
                            <label
                              className={cn(
                                CHECKBOX_LABEL_CLASS,
                                "min-h-9 w-full min-w-0 items-start gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-brand-subtle",
                              )}
                            >
                              <input
                                checked={checked}
                                className={cn(CHECKBOX_INPUT_CLASS, "mt-0.5")}
                                disabled={
                                  pending ||
                                  (!checked && toolLimitReached)
                                }
                                onChange={() => toggleTool(tool.tool_id)}
                                type="checkbox"
                              />
                              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                                <span className="flex min-w-0 flex-wrap items-center gap-2">
                                  <strong>{tool.name}</strong>
                                  <Badge
                                    className="rounded-full"
                                    variant={
                                      tool.requires_approval
                                        ? "brand"
                                        : "neutral"
                                    }
                                  >
                                    {tool.requires_approval
                                      ? "需人工审批"
                                      : "免审批"}
                                  </Badge>
                                </span>
                                {tool.description ? (
                                  <small className="text-xs leading-[1.4] text-text-secondary [overflow-wrap:anywhere]">
                                    {tool.description}
                                  </small>
                                ) : null}
                              </span>
                            </label>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                ))}
              </div>
            )}
            <FieldError
              error={errors.allowed_tool_ids}
              id={`${formId}-tools-error`}
            />
          </fieldset>

          <div className={FORM_GRID_2_CLASS}>
            <div className={FIELD_CLASS}>
              <label htmlFor={`${formId}-max-requests`}>
                最大 LLM 请求次数（可选，1–50）
              </label>
              <input
                aria-describedby={`${formId}-max-requests-error`}
                aria-invalid={Boolean(errors.max_llm_requests)}
                className={FIELD_INPUT_CLASS}
                disabled={pending}
                id={`${formId}-max-requests`}
                inputMode="numeric"
                onChange={(event) =>
                  setValues((current) => ({
                    ...current,
                    max_llm_requests: event.target.value,
                  }))
                }
                placeholder="留空则使用系统默认"
                type="text"
                value={values.max_llm_requests}
              />
              <FieldError
                error={errors.max_llm_requests}
                id={`${formId}-max-requests-error`}
              />
            </div>

            <div className={FIELD_CLASS}>
              <label htmlFor={`${formId}-timeout`}>
                单次执行超时秒数（可选，10–1800）
              </label>
              <input
                aria-describedby={`${formId}-timeout-error`}
                aria-invalid={Boolean(errors.timeout_seconds)}
                className={FIELD_INPUT_CLASS}
                disabled={pending}
                id={`${formId}-timeout`}
                inputMode="numeric"
                onChange={(event) =>
                  setValues((current) => ({
                    ...current,
                    timeout_seconds: event.target.value,
                  }))
                }
                placeholder="留空则使用系统默认"
                type="text"
                value={values.timeout_seconds}
              />
              <FieldError
                error={errors.timeout_seconds}
                id={`${formId}-timeout-error`}
              />
            </div>
          </div>

          {!editing ? (
            <label className={cn(CHECKBOX_LABEL_CLASS, "shrink-0")}>
              <input
                checked={values.enabled}
                className={CHECKBOX_INPUT_CLASS}
                disabled={pending}
                onChange={(event) =>
                  setValues((current) => ({
                    ...current,
                    enabled: event.target.checked,
                  }))
                }
                type="checkbox"
              />
              <span>创建后立即启用该子智能体</span>
            </label>
          ) : null}
        </form>
      )}
    </Modal>
  );
}
