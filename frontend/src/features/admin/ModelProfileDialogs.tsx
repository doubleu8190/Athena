import { useEffect, useId, useMemo, useState } from "react";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { FieldError } from "../../shared/ui/FieldError";
import { Modal } from "../../shared/ui/Modal";
import { adminErrorMessage } from "./errorMessages";
import {
  useCreateModelProfileMutation,
  useSetModelProfileEnabledMutation,
  useUpdateCompressionPolicyMutation,
} from "./hooks";
import {
  compressionPolicyFormBudgets,
  compressionPolicyForm,
  CONTEXT_WINDOW_TOKEN_LIMIT_MAX,
  defaultModelProfileForm,
  OUTPUT_TOKEN_LIMIT_MAX,
  TOKENIZER_ENCODINGS,
  validateCompressionPolicyForm,
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
  const formId = useId();
  const [values, setValues] = useState<ModelProfileFormValues>(
    defaultModelProfileForm,
  );
  const [errors, setErrors] = useState<
    FieldErrors<ModelProfileField>
  >({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateModelProfileMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setValues(defaultModelProfileForm());
      setErrors({});
      setRequestError(null);
      resetMutation();
    }
  }, [open, resetMutation]);

  const close = () => {
    if (!mutation.isPending) {
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

  const handleSubmit = async () => {
    const result = validateModelProfileForm(values);
    setErrors(result.errors);
    if (!result.input) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync(result.input);
      onCreated();
      onClose();
    } catch (failure) {
      setRequestError(adminErrorMessage(failure));
    }
  };

  const fieldId = (name: string) => `${formId}-${name}`;

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="模型版本创建后不可编辑或删除；新版本可用于默认模型或上下文压缩。"
      footer={
        <>
          <button
            className="button-quiet"
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={mutation.isPending}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在创建版本…" : "创建不可变版本"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName="admin-modal admin-model-form-modal"
      title="创建模型版本"
    >
      <form
        className="admin-form"
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <fieldset disabled={mutation.isPending}>
          {/* 双列网格内使用独立 workspace-field 容器包裹 label、input 与错误提示，防止报错节点挤占网格列并保持无障碍标签纯净 */}
          <div className="admin-form-grid">
            <div className="workspace-field">
              <label htmlFor={fieldId("name")}>版本名称</label>
              <input
                aria-describedby={
                  errors.name ? fieldId("name-error") : undefined
                }
                aria-invalid={Boolean(errors.name)}
                autoComplete="off"
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

            <div className="workspace-field">
              <label htmlFor={fieldId("model")}>模型标识</label>
              <input
                aria-describedby={
                  errors.model ? fieldId("model-error") : undefined
                }
                aria-invalid={Boolean(errors.model)}
                autoComplete="off"
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

          <label className="workspace-field" htmlFor={fieldId("base_url")}>
            <span>HTTPS Base URL</span>
            <input
              aria-describedby={
                errors.base_url
                  ? fieldId("base_url-error")
                  : fieldId("base_url-help")
              }
              aria-invalid={Boolean(errors.base_url)}
              autoComplete="url"
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

          <label
            className="workspace-field"
            htmlFor={fieldId("api_key")}
          >
            <span>API Key</span>
            <input
              aria-describedby={
                errors.api_key
                  ? fieldId("api_key-error")
                  : fieldId("api_key-help")
              }
              aria-invalid={Boolean(errors.api_key)}
              autoComplete="off"
              id={fieldId("api_key")}
              onChange={(event) =>
                update("api_key", event.target.value)
              }
              spellCheck={false}
              type="password"
              value={values.api_key}
            />
            <small id={fieldId("api_key-help")}>
              API Key 会加密保存，创建后不会再次显示。
            </small>
          </label>
          <FieldError
            error={errors.api_key}
            id={fieldId("api_key-error")}
          />

          <div className="admin-form-grid">
            <label
              className="workspace-field"
              htmlFor={fieldId("context")}
            >
              <span>上下文窗口 tokens</span>
              <input
                aria-invalid={Boolean(errors.context_window_tokens)}
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
            <label
              className="workspace-field"
              htmlFor={fieldId("max-output")}
            >
              <span>最大输出 tokens</span>
              <input
                aria-invalid={Boolean(errors.max_output_tokens)}
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

          <label
            className="workspace-field"
            htmlFor={fieldId("tokenizer")}
          >
            <span>Tokenizer 编码</span>
            <select
              id={fieldId("tokenizer")}
              onChange={(event) =>
                update(
                  "tokenizer_encoding",
                  event.target.value as ModelProfileFormValues["tokenizer_encoding"],
                )
              }
              value={values.tokenizer_encoding}
            >
              {TOKENIZER_ENCODINGS.map((encoding) => (
                <option key={encoding} value={encoding}>
                  {encoding}
                </option>
              ))}
            </select>
          </label>

          <div className="admin-check-grid">
            <label className="workspace-checkbox">
              <input
                checked={values.supports_stream}
                onChange={(event) =>
                  update("supports_stream", event.target.checked)
                }
                type="checkbox"
              />
              <span>支持流式输出</span>
            </label>
            <label className="workspace-checkbox">
              <input
                checked={values.supports_tools}
                onChange={(event) =>
                  update("supports_tools", event.target.checked)
                }
                type="checkbox"
              />
              <span>支持工具调用</span>
            </label>
            <label className="workspace-checkbox">
              <input
                checked={values.supports_parallel_tool_calls}
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
          </div>
          <FieldError
            error={errors.supports_parallel_tool_calls}
            id={fieldId("parallel-error")}
          />

          <div className="admin-form-grid admin-form-grid--three">
            <label
              className="workspace-field"
              htmlFor={fieldId("request-timeout")}
            >
              <span>请求超时（秒）</span>
              <input
                aria-invalid={Boolean(errors.request_timeout_seconds)}
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
              className="workspace-field"
              htmlFor={fieldId("execution-timeout")}
            >
              <span>执行超时（秒）</span>
              <input
                aria-invalid={Boolean(errors.execution_timeout_seconds)}
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
            <label
              className="workspace-field"
              htmlFor={fieldId("concurrency")}
            >
              <span>最大并发</span>
              <input
                aria-invalid={Boolean(errors.max_concurrency)}
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

          <label
            className="workspace-field"
            htmlFor={fieldId("temperature")}
          >
            <span>temperature（可留空）</span>
            <input
              aria-invalid={Boolean(errors.temperature)}
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

        {mutation.isPending ? (
          <p className="admin-form-status" role="status">
            正在创建不可变模型版本…
          </p>
        ) : null}
        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
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
          <button
            className="button-quiet"
            disabled={mutation.isPending}
            onClick={onClose}
            type="button"
          >
            取消
          </button>
          <button
            className="button-danger"
            disabled={!confirmed || mutation.isPending}
            onClick={() => void handleDisable()}
            type="button"
          >
            {mutation.isPending ? "正在停用…" : "确认停用默认版本"}
          </button>
        </>
      }
      onClose={onClose}
      open={open}
      title="停用当前默认模型"
    >
      <p className="confirm-copy">
        “{profile.name}”仍是默认模型。停用后，后端不会自动选择其他版本，
        新 Run 可能因没有可用默认模型而失败。
      </p>
      <label
        className="workspace-checkbox workspace-checkbox--danger"
        htmlFor={checkboxId}
      >
        <input
          checked={confirmed}
          disabled={mutation.isPending}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我了解新 Run 可能失败，并确认停用该版本</span>
      </label>
      {requestError ? <ErrorBanner message={requestError} /> : null}
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
    () =>
      profiles.filter(
        (profile) => profile.enabled && profile.has_api_key,
      ),
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
          <button
            className="button-quiet"
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={
              mutation.isPending || enabledProfiles.length === 0
            }
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在保存策略…" : "保存压缩策略"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName="admin-modal"
      title="配置上下文压缩"
    >
      <form
        className="admin-form"
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        {enabledProfiles.length === 0 ? (
          <p className="admin-inline-notice" role="status">
            当前没有已启用的模型版本，不能保存压缩策略。
          </p>
        ) : null}
        <label
          className="workspace-field"
          htmlFor={`${formId}-compression-model`}
        >
          <span>压缩模型</span>
          <select
            aria-describedby={
              errors.compression_model_profile_id
                ? `${formId}-compression-model-error`
                : undefined
            }
            aria-invalid={Boolean(
              errors.compression_model_profile_id,
            )}
            disabled={mutation.isPending}
            id={`${formId}-compression-model`}
            onChange={(event) =>
              update(
                "compression_model_profile_id",
                event.target.value,
              )
            }
            value={values.compression_model_profile_id}
          >
            <option value="">请选择已启用模型</option>
            {enabledProfiles.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.name} · {profile.model}
              </option>
            ))}
          </select>
          <small>只列出当前 enabled=true 的模型版本。</small>
        </label>
        <FieldError
          error={errors.compression_model_profile_id}
          id={`${formId}-compression-model-error`}
        />

        <div className="admin-form-grid admin-form-grid--three">
          <label
            className="workspace-field"
            htmlFor={`${formId}-trigger-tokens`}
          >
            <span>触发 Token</span>
            <input
              aria-invalid={Boolean(errors.trigger_tokens)}
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
            className="workspace-field"
            htmlFor={`${formId}-recent-ratio`}
          >
            <span>近期原文比例</span>
            <input
              aria-invalid={Boolean(errors.recent_ratio)}
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
            className="workspace-field"
            htmlFor={`${formId}-recent-token-cap`}
          >
            <span>近期原文 Token 上限</span>
            <input
              aria-invalid={Boolean(errors.recent_token_cap)}
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
            className="workspace-field"
            htmlFor={`${formId}-summary-ratio`}
          >
            <span>摘要比例</span>
            <input
              aria-invalid={Boolean(errors.summary_ratio)}
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
            className="workspace-field"
            htmlFor={`${formId}-summary-token-cap`}
          >
            <span>摘要 Token 上限</span>
            <input
              aria-invalid={Boolean(errors.summary_token_cap)}
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
        <p className="admin-form-help">
          触发 Token 与两个上限必须为正整数；两个比例都必须大于 0
          且小于 1，并且比例之和必须小于 1。
        </p>
        <p className="admin-form-help">
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
          <p className="admin-form-status" role="status">
            正在保存上下文压缩策略…
          </p>
        ) : null}
        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
    </Modal>
  );
}

