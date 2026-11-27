import { useEffect, useId, useMemo, useRef, useState } from "react";

import {
  Button,
  CHECK_GRID_CLASS,
  CHECKBOX_INPUT_CLASS,
  CHECKBOX_INPUT_DANGER_CLASS,
  CHECKBOX_LABEL_CLASS,
  CONFIRM_COPY_CLASS,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_TEXTAREA_CLASS,
  FORM_GRID_2_CLASS,
  FORM_GRID_3_CLASS,
  FORM_STACK_CLASS,
  InlineAlert,
  MODAL_WIDTH_LG,
  MODAL_WIDTH_XL,
  NOTICE_BRAND_CLASS,
  NOTICE_WARNING_CLASS,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { FieldError } from "../../shared/ui/FieldError";
import { Modal } from "../../shared/ui/Modal";
import {
  adminErrorMessage,
  isModelProfileVersionConflict,
} from "./errorMessages";
import {
  useCreateModelProfileMutation,
  useDeleteModelProfileMutation,
  useSetModelProfileEnabledMutation,
  useUpdateCompressionPolicyMutation,
  useUpdateModelProfileMutation,
} from "./hooks";
import {
  compressionPolicyFormBudgets,
  compressionPolicyForm,
  CONTEXT_WINDOW_TOKEN_LIMIT_MAX,
  defaultModelProfileForm,
  modelProfileForm,
  OUTPUT_TOKEN_LIMIT_MAX,
  SCENARIO_DESCRIPTION_MAX_LENGTH,
  SCENARIO_LABEL_MAX_LENGTH,
  TOKENIZER_ENCODINGS,
  validateCompressionPolicyForm,
  validateModelProfileEditForm,
  validateModelProfileForm,
  type CompressionPolicy,
  type CompressionPolicyField,
  type FieldErrors,
  type ModelProfile,
  type ModelProfileField,
  type ModelProfileFormValues,
  type CompressionPolicyFormValues,
} from "./model";
import { formatTokenCount } from "./display";

interface ModelProfileFormDialogProps {
  onClose: () => void;
  onCreated?: () => void;
  onRefreshProfiles?: () => Promise<unknown>;
  open: boolean;
  profile?: ModelProfile | null;
}

function ModelProfileFormDialog({
  onClose,
  onCreated,
  onRefreshProfiles,
  open,
  profile,
}: ModelProfileFormDialogProps) {
  const editing = Boolean(profile);
  const formId = useId();
  const [values, setValues] = useState<ModelProfileFormValues>(() =>
    profile ? modelProfileForm(profile) : defaultModelProfileForm(),
  );
  const [errors, setErrors] = useState<
    FieldErrors<ModelProfileField>
  >({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const [versionConflict, setVersionConflict] = useState(false);
  const [loadedVersionKey, setLoadedVersionKey] = useState<string | null>(
    null,
  );
  const submitLock = useRef(false);
  const createMutation = useCreateModelProfileMutation();
  const updateMutation = useUpdateModelProfileMutation(profile?.id ?? "");
  const { reset: resetCreateMutation } = createMutation;
  const { reset: resetUpdateMutation } = updateMutation;
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
    if (!profile) {
      setValues(defaultModelProfileForm());
      setLoadedVersionKey("create");
    }
  }, [open, profile, resetCreateMutation, resetUpdateMutation]);

  useEffect(() => {
    if (!open || !profile) {
      return;
    }
    const nextKey = `${profile.id}:${profile.version}`;
    if (loadedVersionKey !== nextKey) {
      // 按 id + version 同步最新配置，避免编辑期间后台刷新覆盖未保存草稿。
      setValues(modelProfileForm(profile));
      setLoadedVersionKey(nextKey);
      setErrors({});
      setRequestError(null);
      setVersionConflict(false);
    }
  }, [loadedVersionKey, open, profile]);

  const close = () => {
    if (!pending) {
      onClose();
    }
  };

  const update = <Field extends ModelProfileField>(
    field: Field,
    value: ModelProfileFormValues[Field],
  ) => {
    setValues((current) => {
      if (
        field === "supports_tools" &&
        value === false
      ) {
        return {
          ...current,
          supports_tools: false,
          supports_parallel_tool_calls: false,
        };
      }
      return { ...current, [field]: value };
    });
    setErrors((current) => ({ ...current, [field]: undefined }));
    setRequestError(null);
  };

  const handleRefreshLatest = async () => {
    setRequestError(null);
    setVersionConflict(false);
    setLoadedVersionKey(null);
    if (onRefreshProfiles) {
      await onRefreshProfiles();
    }
  };

  const handleSubmit = async () => {
    if (submitLock.current || pending) {
      return;
    }
    if (editing && profile) {
      const result = validateModelProfileEditForm(values, profile.version);
      setErrors(result.errors);
      if (!result.input) {
        return;
      }
      submitLock.current = true;
      setRequestError(null);
      setVersionConflict(false);
      try {
        await updateMutation.mutateAsync(result.input);
        onClose();
      } catch (failure) {
        if (isModelProfileVersionConflict(failure)) {
          setVersionConflict(true);
        }
        setRequestError(adminErrorMessage(failure));
      } finally {
        submitLock.current = false;
      }
      return;
    }

    const result = validateModelProfileForm(values);
    setErrors(result.errors);
    if (!result.input) {
      return;
    }
    submitLock.current = true;
    setRequestError(null);
    try {
      await createMutation.mutateAsync(result.input);
      onCreated?.();
      onClose();
    } catch (failure) {
      setRequestError(adminErrorMessage(failure));
    } finally {
      submitLock.current = false;
    }
  };

  const fieldId = (name: string) => `${formId}-${name}`;

  return (
    <Modal
      closeDisabled={pending}
      description={
        editing
          ? "局部编辑模型配置与场景展示信息；API Key 留空将保留后端原加密凭据。"
          : "配置模型端点、能力边界、场景展示信息与用户可见策略。"
      }
      footer={
        <>
          <Button
            disabled={pending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={pending}
            form={formId}
            type="submit"
            variant="primary"
          >
            {pending
              ? editing
                ? "正在保存修改…"
                : "正在创建版本…"
              : editing
                ? "保存修改"
                : "创建模型版本"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_LG}
      title={editing ? "编辑模型配置" : "创建模型版本"}
    >
      <form
        className={FORM_STACK_CLASS}
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <fieldset
          className={cn(FORM_STACK_CLASS, "m-0 border-0 p-0")}
          disabled={pending}
        >
          {/* 双列网格内使用独立字段容器包裹 label、input 与错误提示，防止报错节点挤占网格列并保持无障碍标签纯净 */}
          <div className={FORM_GRID_2_CLASS}>
            <div className={FIELD_CLASS}>
              <label htmlFor={fieldId("name")}>版本名称</label>
              <input
                aria-describedby={
                  errors.name ? fieldId("name-error") : undefined
                }
                aria-invalid={Boolean(errors.name)}
                autoComplete="off"
                className={FIELD_INPUT_CLASS}
                data-autofocus
                id={fieldId("name")}
                onChange={(event) => update("name", event.target.value)}
                placeholder="例如：Atlas 主模型 v1"
                value={values.name}
              />
              <FieldError
                error={errors.name}
                id={fieldId("name-error")}
              />
            </div>

            <div className={FIELD_CLASS}>
              <label htmlFor={fieldId("model")}>模型标识</label>
              <input
                aria-describedby={
                  errors.model ? fieldId("model-error") : undefined
                }
                aria-invalid={Boolean(errors.model)}
                autoComplete="off"
                className={FIELD_INPUT_CLASS}
                id={fieldId("model")}
                onChange={(event) => update("model", event.target.value)}
                placeholder="服务端实际接收的 model"
                value={values.model}
              />
              <FieldError
                error={errors.model}
                id={fieldId("model-error")}
              />
            </div>
          </div>

          <label className={FIELD_CLASS} htmlFor={fieldId("base_url")}>
            <span>HTTPS Base URL</span>
            <input
              aria-describedby={
                errors.base_url
                  ? fieldId("base_url-error")
                  : fieldId("base_url-help")
              }
              aria-invalid={Boolean(errors.base_url)}
              autoComplete="url"
              className={FIELD_INPUT_CLASS}
              id={fieldId("base_url")}
              onChange={(event) =>
                update("base_url", event.target.value)
              }
              placeholder="https://llm.example/v1"
              value={values.base_url}
            />
            <small id={fieldId("base_url-help")}>
              必须使用 HTTPS，不能包含账号、查询参数或 URL 片段。
            </small>
          </label>
          <FieldError
            error={errors.base_url}
            id={fieldId("base_url-error")}
          />

          <label className={FIELD_CLASS} htmlFor={fieldId("api_key")}>
            <span>
              {editing
                ? "API Key（可选，留空保留原加密凭据）"
                : "API Key"}
            </span>
            <input
              aria-describedby={
                errors.api_key
                  ? fieldId("api_key-error")
                  : fieldId("api_key-help")
              }
              aria-invalid={Boolean(errors.api_key)}
              autoComplete="off"
              className={FIELD_INPUT_CLASS}
              id={fieldId("api_key")}
              onChange={(event) =>
                update("api_key", event.target.value)
              }
              placeholder={editing ? "留空保留原加密凭据" : undefined}
              spellCheck={false}
              type="password"
              value={values.api_key}
            />
            <small id={fieldId("api_key-help")}>
              {editing
                ? "留空不提交该字段，后端将保留原加密凭据；输入新值则覆盖更新。"
                : "API Key 会加密保存，创建后不会再次显示。"}
            </small>
          </label>
          <FieldError
            error={errors.api_key}
            id={fieldId("api_key-error")}
          />

          <div className={FORM_GRID_2_CLASS}>
            <div className={FIELD_CLASS}>
              <label htmlFor={fieldId("scenario_label")}>
                场景标签（可选）
              </label>
              <input
                aria-describedby={
                  errors.scenario_label
                    ? fieldId("scenario-label-error")
                    : undefined
                }
                aria-invalid={Boolean(errors.scenario_label)}
                autoComplete="off"
                className={FIELD_INPUT_CLASS}
                id={fieldId("scenario_label")}
                maxLength={SCENARIO_LABEL_MAX_LENGTH}
                onChange={(event) =>
                  update("scenario_label", event.target.value)
                }
                placeholder="例如：日常办公、深度思考"
                value={values.scenario_label}
              />
              <FieldError
                error={errors.scenario_label}
                id={fieldId("scenario-label-error")}
              />
            </div>

            <div className={FIELD_CLASS}>
              <label htmlFor={fieldId("tokenizer")}>Tokenizer 编码</label>
              <Select
                // 显式传入 disabled，避免外层 fieldset disabled 时 Radix Select 触发器状态不同步。
                disabled={pending}
                onValueChange={(value) =>
                  update(
                    "tokenizer_encoding",
                    value as ModelProfileFormValues["tokenizer_encoding"],
                  )
                }
                value={values.tokenizer_encoding}
              >
                <SelectTrigger
                  aria-label="Tokenizer 编码"
                  id={fieldId("tokenizer")}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TOKENIZER_ENCODINGS.map((encoding) => (
                    <SelectItem key={encoding} value={encoding}>
                      {encoding}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <label
            className={FIELD_CLASS}
            htmlFor={fieldId("scenario_description")}
          >
            <span>场景说明（可选）</span>
            <textarea
              aria-describedby={
                errors.scenario_description
                  ? fieldId("scenario-description-error")
                  : undefined
              }
              aria-invalid={Boolean(errors.scenario_description)}
              className={FIELD_TEXTAREA_CLASS}
              id={fieldId("scenario_description")}
              maxLength={SCENARIO_DESCRIPTION_MAX_LENGTH}
              onChange={(event) =>
                update("scenario_description", event.target.value)
              }
              placeholder="简述该模型的适用场景，对普通用户展示在模型选择器中（留空则不显示）"
              rows={2}
              value={values.scenario_description}
            />
          </label>
          <FieldError
            error={errors.scenario_description}
            id={fieldId("scenario-description-error")}
          />

          <div className={FORM_GRID_2_CLASS}>
            <label className={FIELD_CLASS} htmlFor={fieldId("context")}>
              <span>上下文窗口 tokens</span>
              <input
                aria-invalid={Boolean(errors.context_window_tokens)}
                className={FIELD_INPUT_CLASS}
                id={fieldId("context")}
                inputMode="numeric"
                max={CONTEXT_WINDOW_TOKEN_LIMIT_MAX}
                min={1}
                onChange={(event) =>
                  update(
                    "context_window_tokens",
                    event.target.value,
                  )
                }
                type="number"
                value={values.context_window_tokens}
              />
            </label>
            <label className={FIELD_CLASS} htmlFor={fieldId("max-output")}>
              <span>最大输出 tokens</span>
              <input
                aria-invalid={Boolean(errors.max_output_tokens)}
                className={FIELD_INPUT_CLASS}
                id={fieldId("max-output")}
                inputMode="numeric"
                max={OUTPUT_TOKEN_LIMIT_MAX}
                min={1}
                onChange={(event) =>
                  update("max_output_tokens", event.target.value)
                }
                type="number"
                value={values.max_output_tokens}
              />
            </label>
          </div>
          <FieldError
            error={
              errors.context_window_tokens ??
              errors.max_output_tokens
            }
            id={fieldId("tokens-error")}
          />

          <div className={CHECK_GRID_CLASS}>
            <label className={CHECKBOX_LABEL_CLASS}>
              <input
                checked={values.supports_stream}
                className={CHECKBOX_INPUT_CLASS}
                onChange={(event) =>
                  update("supports_stream", event.target.checked)
                }
                type="checkbox"
              />
              <span>支持流式输出</span>
            </label>
            <label className={CHECKBOX_LABEL_CLASS}>
              <input
                checked={values.supports_tools}
                className={CHECKBOX_INPUT_CLASS}
                onChange={(event) =>
                  update("supports_tools", event.target.checked)
                }
                type="checkbox"
              />
              <span>支持工具调用</span>
            </label>
            <label className={CHECKBOX_LABEL_CLASS}>
              <input
                checked={values.supports_parallel_tool_calls}
                className={CHECKBOX_INPUT_CLASS}
                disabled={!values.supports_tools}
                onChange={(event) =>
                  update(
                    "supports_parallel_tool_calls",
                    event.target.checked,
                  )
                }
                type="checkbox"
              />
              <span>支持并行工具调用</span>
            </label>
            <label className={CHECKBOX_LABEL_CLASS}>
              <input
                checked={values.supports_vision}
                className={CHECKBOX_INPUT_CLASS}
                onChange={(event) =>
                  update("supports_vision", event.target.checked)
                }
                type="checkbox"
              />
              <span>支持识图 / 多模态</span>
            </label>
            <label className={CHECKBOX_LABEL_CLASS}>
              <input
                checked={values.visible_to_users}
                className={CHECKBOX_INPUT_CLASS}
                onChange={(event) =>
                  update("visible_to_users", event.target.checked)
                }
                type="checkbox"
              />
              <span>对普通用户可见</span>
            </label>
          </div>
          <FieldError
            error={errors.supports_parallel_tool_calls}
            id={fieldId("parallel-error")}
          />

          <div className={FORM_GRID_3_CLASS}>
            <label
              className={FIELD_CLASS}
              htmlFor={fieldId("request-timeout")}
            >
              <span>请求超时（秒）</span>
              <input
                aria-invalid={Boolean(errors.request_timeout_seconds)}
                className={FIELD_INPUT_CLASS}
                id={fieldId("request-timeout")}
                max={3_600}
                min={1}
                onChange={(event) =>
                  update(
                    "request_timeout_seconds",
                    event.target.value,
                  )
                }
                type="number"
                value={values.request_timeout_seconds}
              />
            </label>
            <label
              className={FIELD_CLASS}
              htmlFor={fieldId("execution-timeout")}
            >
              <span>执行超时（秒）</span>
              <input
                aria-invalid={Boolean(errors.execution_timeout_seconds)}
                className={FIELD_INPUT_CLASS}
                id={fieldId("execution-timeout")}
                max={3_600}
                min={1}
                onChange={(event) =>
                  update(
                    "execution_timeout_seconds",
                    event.target.value,
                  )
                }
                type="number"
                value={values.execution_timeout_seconds}
              />
            </label>
            <label className={FIELD_CLASS} htmlFor={fieldId("concurrency")}>
              <span>最大并发</span>
              <input
                aria-invalid={Boolean(errors.max_concurrency)}
                className={FIELD_INPUT_CLASS}
                id={fieldId("concurrency")}
                max={128}
                min={1}
                onChange={(event) =>
                  update("max_concurrency", event.target.value)
                }
                type="number"
                value={values.max_concurrency}
              />
            </label>
          </div>
          <FieldError
            error={
              errors.request_timeout_seconds ??
              errors.execution_timeout_seconds ??
              errors.max_concurrency
            }
            id={fieldId("limits-error")}
          />

          <label className={FIELD_CLASS} htmlFor={fieldId("temperature")}>
            <span>temperature（可留空）</span>
            <input
              aria-invalid={Boolean(errors.temperature)}
              className={FIELD_INPUT_CLASS}
              id={fieldId("temperature")}
              max={2}
              min={0}
              onChange={(event) =>
                update("temperature", event.target.value)
              }
              placeholder="0–2"
              step="0.1"
              type="number"
              value={values.temperature}
            />
          </label>
          <FieldError
            error={errors.temperature}
            id={fieldId("temperature-error")}
          />
        </fieldset>

        {editing && profile ? (
          <p className="text-xs leading-[1.65] text-text-tertiary">
            本次提交的 expected_version：{profile.version}
          </p>
        ) : null}
        {pending ? (
          <p className={NOTICE_BRAND_CLASS} role="status">
            {editing ? "正在保存模型配置…" : "正在创建模型版本…"}
          </p>
        ) : null}
        {requestError ? (
          <InlineAlert
            action={
              versionConflict ? (
                <button
                  className={TEXT_BUTTON_CLASS}
                  onClick={() => void handleRefreshLatest()}
                  type="button"
                >
                  刷新最新配置
                </button>
              ) : undefined
            }
            size="compact"
            variant="danger"
          >
            {requestError}
          </InlineAlert>
        ) : null}
      </form>
    </Modal>
  );
}

interface ModelProfileCreateDialogProps {
  onClose: () => void;
  onCreated: () => void;
  open: boolean;
}

export function ModelProfileCreateDialog({
  onClose,
  onCreated,
  open,
}: ModelProfileCreateDialogProps) {
  return (
    <ModelProfileFormDialog
      onClose={onClose}
      onCreated={onCreated}
      open={open}
    />
  );
}

interface ModelProfileEditDialogProps {
  onClose: () => void;
  onRefreshProfiles: () => Promise<unknown>;
  open: boolean;
  profile: ModelProfile;
}

export function ModelProfileEditDialog({
  onClose,
  onRefreshProfiles,
  open,
  profile,
}: ModelProfileEditDialogProps) {
  return (
    <ModelProfileFormDialog
      onClose={onClose}
      onRefreshProfiles={onRefreshProfiles}
      open={open}
      profile={profile}
    />
  );
}

interface DeleteModelProfileDialogProps {
  onClose: () => void;
  open: boolean;
  profile: ModelProfile;
}

export function DeleteModelProfileDialog({
  onClose,
  open,
  profile,
}: DeleteModelProfileDialogProps) {
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useDeleteModelProfileMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setRequestError(null);
      resetMutation();
    }
  }, [open, profile.id, resetMutation]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    setRequestError(null);
    try {
      await mutation.mutateAsync(profile.id);
      onClose();
    } catch (failure) {
      setRequestError(adminErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="删除后该模型配置将从管理目录与用户模型列表中移除，历史运行记录不受影响。"
      footer={
        <>
          <Button
            disabled={mutation.isPending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={mutation.isPending}
            onClick={() => void handleDelete()}
            type="button"
            variant="danger"
          >
            {mutation.isPending ? "正在删除…" : "确认删除"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      title={`删除模型「${profile.name}」`}
    >
      <p className={CONFIRM_COPY_CLASS}>
        确认删除模型配置 <code>{profile.model}</code>（v{profile.version}）吗？若该模型正被默认模型、压缩策略或活跃子智能体引用，后端将拒绝删除。
      </p>
      {requestError ? (
        <div className="mt-3">
          <InlineAlert size="compact" variant="danger">
            {requestError}
          </InlineAlert>
        </div>
      ) : null}
    </Modal>
  );
}

interface DisableDefaultModelDialogProps {
  onClose: () => void;
  open: boolean;
  profile: ModelProfile;
}

export function DisableDefaultModelDialog({
  onClose,
  open,
  profile,
}: DisableDefaultModelDialogProps) {
  const checkboxId = useId();
  const [confirmed, setConfirmed] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useSetModelProfileEnabledMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      resetMutation();
    }
  }, [open, profile.id, resetMutation]);

  const handleDisable = async () => {
    if (!confirmed) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        enabled: false,
        profileId: profile.id,
      });
      onClose();
    } catch (failure) {
      setRequestError(adminErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="后端不会自动切换默认模型。"
      footer={
        <>
          <Button
            disabled={mutation.isPending}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={!confirmed || mutation.isPending}
            onClick={() => void handleDisable()}
            type="button"
            variant="danger"
          >
            {mutation.isPending ? "正在停用…" : "确认停用默认版本"}
          </Button>
        </>
      }
      onClose={onClose}
      open={open}
      title="停用当前默认模型"
    >
      <p className={CONFIRM_COPY_CLASS}>
        “{profile.name}”仍是默认模型。停用后，后端不会自动选择其他版本，
        新 Run 可能因没有可用默认模型而失败。
      </p>
      <label className={CHECKBOX_LABEL_CLASS} htmlFor={checkboxId}>
        <input
          checked={confirmed}
          className={CHECKBOX_INPUT_DANGER_CLASS}
          disabled={mutation.isPending}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我了解新 Run 可能失败，并确认停用该版本</span>
      </label>
      {requestError ? (
        <InlineAlert size="compact" variant="danger">
          {requestError}
        </InlineAlert>
      ) : null}
    </Modal>
  );
}

interface CompressionPolicyDialogProps {
  onClose: () => void;
  open: boolean;
  policy: CompressionPolicy;
  profiles: ModelProfile[];
}

export function CompressionPolicyDialog({
  onClose,
  open,
  policy,
  profiles,
}: CompressionPolicyDialogProps) {
  const formId = useId();
  const enabledProfiles = useMemo(
    () => profiles.filter((profile) => profile.enabled),
    [profiles],
  );
  const [values, setValues] = useState<CompressionPolicyFormValues>(
    () => compressionPolicyForm(policy),
  );
  const [errors, setErrors] = useState<
    FieldErrors<CompressionPolicyField>
  >({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUpdateCompressionPolicyMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      const initial = compressionPolicyForm(policy);
      if (
        !enabledProfiles.some(
          (profile) =>
            profile.id === initial.compression_model_profile_id,
        )
      ) {
        initial.compression_model_profile_id = "";
      }
      setValues(initial);
      setErrors({});
      setRequestError(null);
      resetMutation();
    }
  }, [enabledProfiles, open, policy, resetMutation]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const update = <Field extends CompressionPolicyField>(
    field: Field,
    value: CompressionPolicyFormValues[Field],
  ) => {
    setValues((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    setRequestError(null);
  };

  const handleSubmit = async () => {
    const result = validateCompressionPolicyForm(
      values,
      enabledProfiles,
    );
    setErrors(result.errors);
    if (!result.input) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync(result.input);
      onClose();
    } catch (failure) {
      setRequestError(adminErrorMessage(failure));
    }
  };
  const derivedBudgets = compressionPolicyFormBudgets(values);

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="保存会创建新的不可变策略版本；上下文压缩没有独立关闭开关。"
      footer={
        <>
          <Button
            disabled={mutation.isPending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={
              mutation.isPending || enabledProfiles.length === 0
            }
            form={formId}
            type="submit"
            variant="primary"
          >
            {mutation.isPending ? "正在保存策略…" : "保存压缩策略"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_XL}
      title="配置上下文压缩"
    >
      <form
        className={FORM_STACK_CLASS}
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        {enabledProfiles.length === 0 ? (
          <p className={NOTICE_WARNING_CLASS} role="status">
            当前没有已启用的模型版本，不能保存压缩策略。
          </p>
        ) : null}
        <div className={FIELD_CLASS}>
          <label htmlFor={`${formId}-compression-model`}>压缩模型</label>
          <Select
            disabled={mutation.isPending}
            onValueChange={(value) =>
              update("compression_model_profile_id", value)
            }
            value={values.compression_model_profile_id}
          >
            <SelectTrigger
              aria-describedby={
                errors.compression_model_profile_id
                  ? `${formId}-compression-model-error`
                  : undefined
              }
              aria-label="压缩模型"
              error={Boolean(errors.compression_model_profile_id)}
              id={`${formId}-compression-model`}
            >
              <SelectValue placeholder="请选择已启用模型" />
            </SelectTrigger>
            <SelectContent>
              {enabledProfiles.map((profile) => (
                <SelectItem key={profile.id} value={profile.id}>
                  {profile.name} · {profile.model}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <small>只列出当前 enabled=true 的模型版本。</small>
        </div>
        <FieldError
          error={errors.compression_model_profile_id}
          id={`${formId}-compression-model-error`}
        />

        <div className={FORM_GRID_3_CLASS}>
          <label
            className={FIELD_CLASS}
            htmlFor={`${formId}-trigger-tokens`}
          >
            <span>触发 Token</span>
            <input
              aria-invalid={Boolean(errors.trigger_tokens)}
              className={FIELD_INPUT_CLASS}
              disabled={mutation.isPending}
              id={`${formId}-trigger-tokens`}
              min={1}
              onChange={(event) =>
                update("trigger_tokens", event.target.value)
              }
              step="1"
              type="number"
              value={values.trigger_tokens}
            />
          </label>
          <label
            className={FIELD_CLASS}
            htmlFor={`${formId}-recent-ratio`}
          >
            <span>近期原文比例</span>
            <input
              aria-invalid={Boolean(errors.recent_ratio)}
              className={FIELD_INPUT_CLASS}
              disabled={mutation.isPending}
              id={`${formId}-recent-ratio`}
              max={0.999}
              min={0.001}
              onChange={(event) =>
                update("recent_ratio", event.target.value)
              }
              step="0.01"
              type="number"
              value={values.recent_ratio}
            />
          </label>
          <label
            className={FIELD_CLASS}
            htmlFor={`${formId}-recent-token-cap`}
          >
            <span>近期原文 Token 上限</span>
            <input
              aria-invalid={Boolean(errors.recent_token_cap)}
              className={FIELD_INPUT_CLASS}
              disabled={mutation.isPending}
              id={`${formId}-recent-token-cap`}
              min={1}
              onChange={(event) =>
                update("recent_token_cap", event.target.value)
              }
              step="1"
              type="number"
              value={values.recent_token_cap}
            />
          </label>
          <label
            className={FIELD_CLASS}
            htmlFor={`${formId}-summary-ratio`}
          >
            <span>摘要比例</span>
            <input
              aria-invalid={Boolean(errors.summary_ratio)}
              className={FIELD_INPUT_CLASS}
              disabled={mutation.isPending}
              id={`${formId}-summary-ratio`}
              max={0.999}
              min={0.001}
              onChange={(event) =>
                update("summary_ratio", event.target.value)
              }
              step="0.01"
              type="number"
              value={values.summary_ratio}
            />
          </label>
          <label
            className={FIELD_CLASS}
            htmlFor={`${formId}-summary-token-cap`}
          >
            <span>摘要 Token 上限</span>
            <input
              aria-invalid={Boolean(errors.summary_token_cap)}
              className={FIELD_INPUT_CLASS}
              disabled={mutation.isPending}
              id={`${formId}-summary-token-cap`}
              min={1}
              onChange={(event) =>
                update("summary_token_cap", event.target.value)
              }
              step="1"
              type="number"
              value={values.summary_token_cap}
            />
          </label>
        </div>
        <p className="text-xs leading-[1.65] text-text-tertiary">
          触发 Token 与两个上限必须为正整数；两个比例都必须大于 0
          且小于 1，并且比例之和必须小于 1。
        </p>
        <p className="text-xs leading-[1.65] text-text-tertiary">
          派生近期原文预算：{formatTokenCount(derivedBudgets.recent)}
          {" · "}
          派生摘要预算：{formatTokenCount(derivedBudgets.summary)}
        </p>
        <FieldError
          error={
            errors.trigger_tokens ??
            errors.recent_ratio ??
            errors.recent_token_cap ??
            errors.summary_ratio ??
            errors.summary_token_cap
          }
          id={`${formId}-ratios-error`}
        />
        {mutation.isPending ? (
          <p className={NOTICE_BRAND_CLASS} role="status">
            正在保存上下文压缩策略…
          </p>
        ) : null}
        {requestError ? (
          <InlineAlert size="compact" variant="danger">
            {requestError}
          </InlineAlert>
        ) : null}
      </form>
    </Modal>
  );
}

