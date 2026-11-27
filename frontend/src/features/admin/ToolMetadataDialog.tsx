import { Plus, Sparkles, Trash2 } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { getAuthIdentityEpoch } from "../../shared/api/client";
import {
  Button,
  EMPTY_INLINE_CLASS,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_TEXTAREA_CLASS,
  FORM_STACK_CLASS,
  ICON_BUTTON_CLASS,
  ICON_BUTTON_DANGER_CLASS,
  InlineAlert,
  MODAL_WIDTH_XL,
  NOTICE_BRAND_CLASS,
  cn,
} from "../../shared/ui";
import { FieldError } from "../../shared/ui/FieldError";
import { Modal } from "../../shared/ui/Modal";
import { adminErrorMessage } from "./errorMessages";
import { useUpdateAdminToolMetadataMutation } from "./hooks";
import {
  APPROVAL_SUMMARY_TEMPLATE_MAX_LENGTH,
  TOOL_DISPLAY_NAME_MAX_LENGTH,
  extractToolSchemaParamNames,
  toolMetadataForm,
  validateToolMetadataForm,
  type AdminTool,
  type FieldErrors,
  type ToolMetadataField,
  type ToolMetadataFormValues,
  type ToolParameterLabelRow,
} from "./model";

let labelRowSeq = 0;

function newToolLabelRow(
  prefix: string,
  key = "",
  label = "",
): ToolParameterLabelRow {
  labelRowSeq += 1;
  return {
    id: `${prefix}-${labelRowSeq}`,
    key,
    label,
  };
}

interface ToolLabelEditorSectionProps {
  addLabel: string;
  description: string;
  emptyText: string;
  error?: string;
  errorId: string;
  extraAction?: ReactNode;
  keyLabelPrefix: string;
  keyPlaceholder: string;
  labelPrefix: string;
  labelPlaceholder: string;
  onAdd: () => void;
  onChangeRow: (rowId: string, patch: { key?: string; label?: string }) => void;
  onRemoveRow: (rowId: string) => void;
  pending: boolean;
  removeTitle: string;
  rows: ToolParameterLabelRow[];
  title: string;
  titleId: string;
}

/** 复用于入参白名单（parameter_labels）与出参白名单（result_labels）的键值标签行编辑器。 */
function ToolLabelEditorSection({
  addLabel,
  description,
  emptyText,
  error,
  errorId,
  extraAction,
  keyLabelPrefix,
  keyPlaceholder,
  labelPrefix,
  labelPlaceholder,
  onAdd,
  onChangeRow,
  onRemoveRow,
  pending,
  removeTitle,
  rows,
  title,
  titleId,
}: ToolLabelEditorSectionProps) {
  return (
    <section
      aria-labelledby={titleId}
      className="rounded-[10px] border border-border-default bg-subtle p-[13px]"
    >
      <header className="mb-2.5 flex flex-col items-start justify-between gap-2.5 sm:flex-row sm:items-center">
        <div>
          <h3 className="text-xs font-bold text-text-primary" id={titleId}>
            {title}
          </h3>
          <p className="mt-[3px] text-xs text-text-tertiary">{description}</p>
        </div>
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
          {extraAction}
          <Button
            disabled={pending}
            onClick={onAdd}
            size="sm"
            type="button"
            variant="outline"
          >
            <Plus
              aria-hidden="true"
              focusable="false"
              size={13}
              strokeWidth={1.7}
            />
            {addLabel}
          </Button>
        </div>
      </header>

      {rows.length === 0 ? (
        <p className={EMPTY_INLINE_CLASS}>{emptyText}</p>
      ) : (
        <div className="flex flex-col gap-[9px]">
          {rows.map((row, index) => (
            <div
              className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2 rounded-[9px] border border-border-default bg-canvas p-2.5 sm:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)_auto] sm:items-end"
              key={row.id}
            >
              <label
                className={cn(FIELD_CLASS, "col-start-1 sm:col-start-auto")}
                htmlFor={`${row.id}-key`}
              >
                <span>
                  {keyLabelPrefix} {index + 1}
                </span>
                <input
                  autoComplete="off"
                  className={FIELD_INPUT_CLASS}
                  id={`${row.id}-key`}
                  onChange={(event) =>
                    onChangeRow(row.id, { key: event.target.value })
                  }
                  placeholder={keyPlaceholder}
                  spellCheck={false}
                  value={row.key}
                />
              </label>
              <label
                className={cn(
                  FIELD_CLASS,
                  "col-start-1 row-start-2 sm:col-start-auto sm:row-start-auto",
                )}
                htmlFor={`${row.id}-label`}
              >
                <span>
                  {labelPrefix} {index + 1}
                </span>
                <input
                  autoComplete="off"
                  className={FIELD_INPUT_CLASS}
                  id={`${row.id}-label`}
                  onChange={(event) =>
                    onChangeRow(row.id, { label: event.target.value })
                  }
                  placeholder={labelPlaceholder}
                  value={row.label}
                />
              </label>
              <button
                aria-label={`${removeTitle} ${row.key || index + 1}`}
                className={cn(
                  ICON_BUTTON_CLASS,
                  ICON_BUTTON_DANGER_CLASS,
                  "col-start-2 row-start-1 mt-[30px] sm:col-start-auto sm:row-start-auto sm:mt-0 sm:mb-[4.5px]",
                )}
                disabled={pending}
                onClick={() => onRemoveRow(row.id)}
                title={removeTitle}
                type="button"
              >
                <Trash2
                  aria-hidden="true"
                  focusable="false"
                  size={15}
                  strokeWidth={1.7}
                />
              </button>
            </div>
          ))}
        </div>
      )}
      <FieldError error={error} id={errorId} />
    </section>
  );
}

export interface ToolMetadataDialogProps {
  onClose: () => void;
  open: boolean;
  tool: AdminTool;
}

export function ToolMetadataDialog({
  onClose,
  open,
  tool,
}: ToolMetadataDialogProps) {
  const formId = useId();
  const [values, setValues] = useState<ToolMetadataFormValues>(() =>
    toolMetadataForm(tool),
  );
  const [errors, setErrors] = useState<FieldErrors<ToolMetadataField>>({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const submitLock = useRef(false);
  const mutation = useUpdateAdminToolMetadataMutation(tool.tool_id);
  const { reset: resetMutation } = mutation;
  const pending = mutation.isPending;

  const schemaParamNames = useMemo(
    () => extractToolSchemaParamNames(tool.input_schema),
    [tool.input_schema],
  );

  useEffect(() => {
    if (open) {
      setValues(toolMetadataForm(tool));
      setErrors({});
      setRequestError(null);
      resetMutation();
      submitLock.current = false;
    }
  }, [open, resetMutation, tool]);

  const missingSchemaParams = useMemo(() => {
    const existingKeys = new Set(
      values.parameter_labels.map((row) => row.key.trim()).filter(Boolean),
    );
    return schemaParamNames.filter((name) => !existingKeys.has(name));
  }, [schemaParamNames, values.parameter_labels]);

  const close = () => {
    if (!pending) {
      onClose();
    }
  };

  const handleImportSchemaParams = () => {
    if (missingSchemaParams.length === 0) {
      return;
    }
    setValues((current) => ({
      ...current,
      parameter_labels: [
        ...current.parameter_labels,
        ...missingSchemaParams.map((name) =>
          newToolLabelRow("tool-param", name, ""),
        ),
      ],
    }));
    setErrors((current) => ({ ...current, parameter_labels: undefined }));
    setRequestError(null);
  };

  const addLabelRow = (field: "parameter_labels" | "result_labels") => {
    const prefix = field === "parameter_labels" ? "tool-param" : "tool-result";
    setValues((current) => ({
      ...current,
      [field]: [...current[field], newToolLabelRow(prefix)],
    }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    setRequestError(null);
  };

  const updateLabelRow = (
    field: "parameter_labels" | "result_labels",
    rowId: string,
    patch: { key?: string; label?: string },
  ) => {
    setValues((current) => ({
      ...current,
      [field]: current[field].map((item) =>
        item.id === rowId ? { ...item, ...patch } : item,
      ),
    }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    setRequestError(null);
  };

  const removeLabelRow = (
    field: "parameter_labels" | "result_labels",
    rowId: string,
  ) => {
    setValues((current) => ({
      ...current,
      [field]: current[field].filter((item) => item.id !== rowId),
    }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    setRequestError(null);
  };

  const handleAppendPlaceholder = (paramName: string) => {
    const placeholder = `{${paramName}}`;
    setValues((current) => {
      const currentText = current.approval_summary_template;
      const separator =
        currentText && !/\s$/u.test(currentText) ? " " : "";
      return {
        ...current,
        approval_summary_template: `${currentText}${separator}${placeholder}`,
      };
    });
    setErrors((current) => ({
      ...current,
      approval_summary_template: undefined,
    }));
    setRequestError(null);
  };

  const handleSubmit = async () => {
    if (submitLock.current || pending) {
      return;
    }
    const result = validateToolMetadataForm(values);
    setErrors(result.errors);
    if (!result.input) {
      return;
    }
    submitLock.current = true;
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        identityEpoch: getAuthIdentityEpoch(),
        input: result.input,
      });
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
      description={`配置工具 ${tool.remote_name}（${tool.model_name}）在审批卡片与工具列表中的业务展示信息。`}
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
            {pending ? "正在保存…" : "保存业务展示"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_XL}
      title="编辑工具业务展示"
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
          <div className={FIELD_CLASS}>
            <label htmlFor={fieldId("display-name")}>中文展示名（可选）</label>
            <input
              aria-describedby={
                errors.display_name
                  ? fieldId("display-name-error")
                  : fieldId("display-name-help")
              }
              aria-invalid={Boolean(errors.display_name)}
              autoComplete="off"
              className={FIELD_INPUT_CLASS}
              data-autofocus
              id={fieldId("display-name")}
              maxLength={TOOL_DISPLAY_NAME_MAX_LENGTH}
              onChange={(event) => {
                setValues((current) => ({
                  ...current,
                  display_name: event.target.value,
                }));
                setErrors((current) => ({
                  ...current,
                  display_name: undefined,
                }));
                setRequestError(null);
              }}
              placeholder="例如：创建报销审批单"
              value={values.display_name}
            />
            <small id={fieldId("display-name-help")}>
              留空保存将清空中文展示名，并回退显示远端工具名（最多{" "}
              {TOOL_DISPLAY_NAME_MAX_LENGTH} 字）。
            </small>
          </div>
          <FieldError
            error={errors.display_name}
            id={fieldId("display-name-error")}
          />

          <div className={FIELD_CLASS}>
            <label htmlFor={fieldId("summary-template")}>
              审批摘要模板（可选）
            </label>
            <textarea
              aria-describedby={
                errors.approval_summary_template
                  ? fieldId("summary-template-error")
                  : fieldId("summary-template-help")
              }
              aria-invalid={Boolean(errors.approval_summary_template)}
              className={FIELD_TEXTAREA_CLASS}
              id={fieldId("summary-template")}
              maxLength={APPROVAL_SUMMARY_TEMPLATE_MAX_LENGTH}
              onChange={(event) => {
                setValues((current) => ({
                  ...current,
                  approval_summary_template: event.target.value,
                }));
                setErrors((current) => ({
                  ...current,
                  approval_summary_template: undefined,
                }));
                setRequestError(null);
              }}
              placeholder="例如：向 {payee} 发起 {amount} 元付款"
              rows={3}
              value={values.approval_summary_template}
            />
            <small id={fieldId("summary-template-help")}>
              支持 <code>{"{参数名}"}</code>{" "}
              占位符在待审批卡片生成中文摘要；留空保存将清空模板（最多{" "}
              {APPROVAL_SUMMARY_TEMPLATE_MAX_LENGTH} 字）。
            </small>
            {schemaParamNames.length > 0 ? (
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-text-tertiary">
                  可选参数占位符：
                </span>
                {schemaParamNames.map((paramName) => (
                  <button
                    className="inline-flex items-center rounded-md border border-border-default bg-subtle px-2 py-0.5 font-mono text-xs text-text-secondary transition-colors hover:border-brand-border hover:bg-brand-subtle hover:text-brand"
                    key={paramName}
                    onClick={() => handleAppendPlaceholder(paramName)}
                    type="button"
                  >
                    {`{${paramName}}`}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
          <FieldError
            error={errors.approval_summary_template}
            id={fieldId("summary-template-error")}
          />

          <ToolLabelEditorSection
            addLabel="添加参数"
            description="将工具入参键名映射为审批卡片与步骤详情中的中文标签，并同时作为普通用户的入参展示白名单；仅已配置字段会向普通用户展示，未配置参数将自动隐藏。"
            emptyText="暂未配置参数中文标签。可点击“添加参数”或从 Schema 一键带入。"
            error={errors.parameter_labels}
            errorId={fieldId("param-labels-error")}
            extraAction={
              schemaParamNames.length > 0 ? (
                <Button
                  disabled={pending || missingSchemaParams.length === 0}
                  onClick={handleImportSchemaParams}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <Sparkles
                    aria-hidden="true"
                    focusable="false"
                    size={13}
                    strokeWidth={1.7}
                  />
                  一键带入 Schema 参数
                </Button>
              ) : null
            }
            keyLabelPrefix="参数键名"
            keyPlaceholder="例如：payee"
            labelPrefix="中文标签"
            labelPlaceholder="例如：收款人"
            onAdd={() => addLabelRow("parameter_labels")}
            onChangeRow={(rowId, patch) =>
              updateLabelRow("parameter_labels", rowId, patch)
            }
            onRemoveRow={(rowId) => removeLabelRow("parameter_labels", rowId)}
            pending={pending}
            removeTitle="移除参数"
            rows={values.parameter_labels}
            title="参数中文标签"
            titleId={fieldId("param-labels-title")}
          />

          <ToolLabelEditorSection
            addLabel="添加结果字段"
            description="默认隐藏全部原始出参；仅已配置字段会从工具返回的结构化结果中按白名单向普通用户展示。"
            emptyText="暂未配置可展示结果字段，默认隐藏全部原始出参。可点击“添加结果字段”配置出参白名单。"
            error={errors.result_labels}
            errorId={fieldId("result-labels-error")}
            keyLabelPrefix="结果字段键名"
            keyPlaceholder="例如：order_no"
            labelPrefix="结果中文标签"
            labelPlaceholder="例如：审批单号"
            onAdd={() => addLabelRow("result_labels")}
            onChangeRow={(rowId, patch) =>
              updateLabelRow("result_labels", rowId, patch)
            }
            onRemoveRow={(rowId) => removeLabelRow("result_labels", rowId)}
            pending={pending}
            removeTitle="移除结果字段"
            rows={values.result_labels}
            title="可展示结果字段（出参白名单）"
            titleId={fieldId("result-labels-title")}
          />
        </fieldset>

        {pending ? (
          <p className={NOTICE_BRAND_CLASS} role="status">
            正在保存工具业务展示元信息…
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
