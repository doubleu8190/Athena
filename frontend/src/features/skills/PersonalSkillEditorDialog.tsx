import { RefreshCw, Sparkle } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { newRequestId } from "../../shared/lib/requestId";
import {
  Button,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_TEXTAREA_CLASS,
  InlineAlert,
  MODAL_WIDTH_2XL,
  MarkdownSplitEditor,
  NOTICE_BRAND_CLASS,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { Modal } from "../../shared/ui/Modal";
import {
  isSkillRevisionConflict,
  skillErrorMessage,
} from "./errorMessages";
import {
  useCreatePersonalSkillMutation,
  useParsePersonalSkillMutation,
  usePersonalSkill,
  useSavePersonalSkillVersionMutation,
} from "./hooks";
import {
  PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH,
  PERSONAL_SKILL_GUIDE_MAX_LENGTH,
  PERSONAL_SKILL_NAME_MAX_LENGTH,
  normalizePersonalSkillInput,
  validatePersonalSkillFile,
  validatePersonalSkillInput,
  type PersonalSkillFieldErrors,
  type PersonalSkillInput,
  type SkillAdminDetails,
} from "./model";

type EditorMode = "create" | "edit";
type InputMode = "manual" | "import";

interface PersonalSkillEditorDialogProps {
  mode: EditorMode;
  onClose: () => void;
  onSaved: (details: SkillAdminDetails, mode: EditorMode) => void;
  open: boolean;
  skillId?: string;
}

const EMPTY_INPUT: PersonalSkillInput = {
  description: "",
  guide: "",
  name: "",
};

function errorDescription(...ids: Array<string | false | undefined>) {
  const value = ids.filter(Boolean).join(" ");
  return value || undefined;
}

export function PersonalSkillEditorDialog({
  mode,
  onClose,
  onSaved,
  open,
  skillId,
}: PersonalSkillEditorDialogProps) {
  const formId = useId();
  const nameId = useId();
  const descriptionId = useId();
  const guideId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const fileInputId = useId();
  const requestIdRef = useRef<string | null>(null);
  const initializedRef = useRef(false);
  const [inputMode, setInputMode] = useState<InputMode>("manual");
  const [values, setValues] = useState<PersonalSkillInput>(EMPTY_INPUT);
  const [fieldErrors, setFieldErrors] = useState<PersonalSkillFieldErrors>(
    {},
  );
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [importedMessage, setImportedMessage] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [baselineRevision, setBaselineRevision] = useState<number | null>(
    null,
  );

  const detail = usePersonalSkill(
    open && mode === "edit" ? (skillId ?? null) : null,
  );
  const parseMutation = useParsePersonalSkillMutation();
  const createMutation = useCreatePersonalSkillMutation();
  const saveMutation = useSavePersonalSkillVersionMutation(skillId ?? "");
  const { reset: resetParseMutation } = parseMutation;
  const { reset: resetCreateMutation } = createMutation;
  const { reset: resetSaveMutation } = saveMutation;
  const savePending = createMutation.isPending || saveMutation.isPending;
  const fieldsDisabled = savePending || parseMutation.isPending;

  useEffect(() => {
    if (!open) {
      initializedRef.current = false;
      return;
    }
    setInputMode("manual");
    setFileError(null);
    setImportedMessage(null);
    setRequestError(null);
    setRevisionConflict(false);
    setFieldErrors({});
    setSelectedFile(null);
    requestIdRef.current = null;
    resetParseMutation();
    resetCreateMutation();
    resetSaveMutation();
    if (mode === "create") {
      setValues(EMPTY_INPUT);
      setBaselineRevision(null);
      initializedRef.current = true;
    } else {
      initializedRef.current = false;
    }
    // 弹窗每次打开都从独立草稿开始，失败输入只保留在当前打开周期内。
  }, [
    mode,
    open,
    resetCreateMutation,
    resetParseMutation,
    resetSaveMutation,
  ]);

  useEffect(() => {
    if (
      !open ||
      mode !== "edit" ||
      !detail.data ||
      initializedRef.current
    ) {
      return;
    }
    const currentVersion = detail.data.versions.find(
      (version) => version.id === detail.data?.current_version_id,
    );
    setValues({
      description: detail.data.description,
      guide: currentVersion?.guide ?? "",
      name: detail.data.name,
    });
    setBaselineRevision(detail.data.revision);
    initializedRef.current = true;
  }, [detail.data, mode, open]);

  const updateField = (key: keyof PersonalSkillInput, value: string) => {
    setValues((current) => ({ ...current, [key]: value }));
    setFieldErrors((current) => ({ ...current, [key]: undefined }));
    setRequestError(null);
    setRevisionConflict(false);
    requestIdRef.current = null;
  };

  const close = () => {
    if (!savePending && !parseMutation.isPending) {
      onClose();
    }
  };

  const handleFileSelection = async (file: File | null) => {
    setSelectedFile(file);
    setImportedMessage(null);
    setRequestError(null);
    setRevisionConflict(false);
    const validationError = validatePersonalSkillFile(file);
    setFileError(validationError);
    if (validationError || !file) {
      return;
    }

    try {
      const draft = await parseMutation.mutateAsync(file);
      setValues({
        description: draft.description,
        guide: draft.guide,
        name: draft.name,
      });
      setFieldErrors({});
      setImportedMessage(
        "文件已填入下方表单，尚未发布。请核对内容并点击保存。",
      );
      requestIdRef.current = null;
    } catch (error) {
      // 解析失败只提示文件问题，保留用户当前已经填写或导入的表单内容。
      setRequestError(skillErrorMessage(error));
    }
  };

  const handleSubmit = async () => {
    const nextErrors = validatePersonalSkillInput(values);
    setFieldErrors(nextErrors);
    setRequestError(null);
    setRevisionConflict(false);
    if (Object.keys(nextErrors).length > 0) {
      return;
    }

    const normalized = normalizePersonalSkillInput(values);
    const requestId = requestIdRef.current ?? newRequestId();
    requestIdRef.current = requestId;
    try {
      let saved: SkillAdminDetails;
      if (mode === "create") {
        saved = await createMutation.mutateAsync({
          input: normalized,
          requestId,
        });
      } else {
        if (baselineRevision === null) {
          throw new Error("个人技能详情尚未加载完成。");
        }
        saved = await saveMutation.mutateAsync({
          expectedRevision: baselineRevision,
          input: normalized,
          requestId,
        });
      }
      requestIdRef.current = null;
      onSaved(saved, mode);
    } catch (error) {
      setRequestError(skillErrorMessage(error));
      setRevisionConflict(
        mode === "edit" && isSkillRevisionConflict(error),
      );
    }
  };

  const refreshLatest = async () => {
    const result = await detail.refetch();
    if (!result.data) {
      if (result.error) {
        setRequestError(skillErrorMessage(result.error));
      }
      return;
    }
    const currentVersion = result.data.versions.find(
      (version) => version.id === result.data?.current_version_id,
    );
    setValues({
      description: result.data.description,
      guide: currentVersion?.guide ?? "",
      name: result.data.name,
    });
    setBaselineRevision(result.data.revision);
    setFieldErrors({});
    setRequestError(null);
    setRevisionConflict(false);
    setImportedMessage("已加载最新版本内容，请核对后重新保存。");
    requestIdRef.current = null;
  };

  const title =
    mode === "create" ? "新建个人技能" : "编辑个人技能";
  const loadingDetail = mode === "edit" && detail.isPending;
  const detailUnavailable = mode === "edit" && detail.isError;

  return (
    <Modal
      closeDisabled={savePending || parseMutation.isPending}
      description="个人技能仅本人可见。保存后立即启用，下一次新任务会读取最新版本。"
      footer={
        <>
          <Button
            disabled={savePending || parseMutation.isPending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={
              savePending ||
              parseMutation.isPending ||
              loadingDetail ||
              detailUnavailable
            }
            form={formId}
            type="submit"
            variant="primary"
          >
            {savePending
              ? "正在保存…"
              : mode === "create"
                ? "创建并启用"
                : "保存并启用新版本"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_2XL}
      title={title}
    >
      {loadingDetail ? (
        <div
          className="grid min-h-[220px] place-items-center text-[13px] text-text-secondary"
          role="status"
        >
          正在读取技能最新内容…
        </div>
      ) : detailUnavailable ? (
        <InlineAlert
          action={
            <button
              className={TEXT_BUTTON_CLASS}
              onClick={() => void detail.refetch()}
              type="button"
            >
              重试
            </button>
          }
          size="compact"
          variant="danger"
        >
          {skillErrorMessage(detail.error)}
        </InlineAlert>
      ) : (
        <form
          className="flex min-w-0 flex-col gap-[15px]"
          id={formId}
          onSubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
        >
          <div className="flex flex-col items-stretch justify-between gap-4 rounded-[10px] border border-border-default bg-subtle px-[13px] py-3 sm:flex-row sm:items-center">
            <div>
              <strong className="text-[13px] text-text-primary">内容来源</strong>
              <p className="mt-[3px] text-xs leading-[1.55] text-text-tertiary">
                在线编写，或先导入一个 Markdown 文件继续编辑。
              </p>
            </div>
            <div
              aria-label="个人技能内容来源"
              className="inline-flex w-full shrink-0 gap-[3px] rounded-[9px] border border-border-default bg-muted p-[3px] sm:w-auto"
              role="group"
            >
              <button
                aria-pressed={inputMode === "manual"}
                className={cn(
                  "min-h-8 flex-1 rounded-md px-[11px] text-xs font-semibold transition-colors sm:flex-none",
                  inputMode === "manual"
                    ? "bg-canvas text-brand shadow-xs"
                    : "bg-transparent text-text-secondary",
                )}
                disabled={savePending || parseMutation.isPending}
                onClick={() => setInputMode("manual")}
                type="button"
              >
                在线编写
              </button>
              <button
                aria-pressed={inputMode === "import"}
                className={cn(
                  "min-h-8 flex-1 rounded-md px-[11px] text-xs font-semibold transition-colors sm:flex-none",
                  inputMode === "import"
                    ? "bg-canvas text-brand shadow-xs"
                    : "bg-transparent text-text-secondary",
                )}
                disabled={savePending || parseMutation.isPending}
                onClick={() => setInputMode("import")}
                type="button"
              >
                导入 Markdown
              </button>
            </div>
          </div>

          {inputMode === "import" ? (
            <div className="flex min-w-0 flex-col gap-1.5 rounded-[10px] border border-dashed border-brand-border bg-brand-subtle p-[13px]">
              <label
                className="text-[13px] font-semibold text-text-primary"
                htmlFor={fileInputId}
              >
                选择单个 Markdown 文件
              </label>
              <input
                accept=".md"
                aria-describedby={errorDescription(
                  `${fileInputId}-help`,
                  fileError ? `${fileInputId}-error` : undefined,
                )}
                aria-invalid={fileError ? true : undefined}
                className="w-full min-w-0 text-xs text-text-secondary"
                disabled={savePending || parseMutation.isPending}
                id={fileInputId}
                onChange={(event) => {
                  void handleFileSelection(
                    event.currentTarget.files?.[0] ?? null,
                  );
                }}
                type="file"
              />
              <small
                className="text-xs leading-[1.55] text-text-tertiary"
                id={`${fileInputId}-help`}
              >
                仅接受 .md 扩展名，大小 1 B–1 MiB；导入只填充表单，不会自动发布。
              </small>
              {selectedFile ? (
                <span className="font-mono text-xs text-brand-hover [overflow-wrap:anywhere]">
                  {selectedFile.name}
                </span>
              ) : null}
            </div>
          ) : null}

          {/* 将文件校验错误、解析进度与导入提示置于模式分支之外，确保「在线编写」下使用编辑器工具栏「导入 .md」也能完整反馈。 */}
          {fileError ? (
            <p
              className="field-error"
              id={`${fileInputId}-error`}
              role="alert"
            >
              {fileError}
            </p>
          ) : null}
          {parseMutation.isPending ? (
            <p
              className="flex items-start gap-1.5 text-xs leading-[1.55] text-brand-hover"
              role="status"
            >
              正在解析 Markdown…
            </p>
          ) : null}
          {importedMessage ? (
            <p
              className="flex items-start gap-1.5 text-xs leading-[1.55] text-brand-hover [&>svg]:mt-px [&>svg]:shrink-0"
              role="status"
            >
              <Sparkle
                aria-hidden="true"
                focusable="false"
                size={14}
                strokeWidth={1.7}
              />
              {importedMessage}
            </p>
          ) : null}

          <div className="flex min-w-0 flex-col gap-3.5">
            <div className={FIELD_CLASS}>
              <label htmlFor={nameId}>技能名称</label>
              <input
                aria-describedby={errorDescription(
                  `${nameId}-help`,
                  fieldErrors.name ? nameErrorId : undefined,
                )}
                aria-invalid={fieldErrors.name ? true : undefined}
                autoComplete="off"
                className={FIELD_INPUT_CLASS}
                data-autofocus={mode === "create" ? true : undefined}
                disabled={fieldsDisabled}
                id={nameId}
                maxLength={PERSONAL_SKILL_NAME_MAX_LENGTH}
                onChange={(event) => updateField("name", event.target.value)}
                placeholder="例如：周报整理"
                value={values.name}
              />
              <small id={`${nameId}-help`}>
                {Array.from(values.name).length} /{" "}
                {PERSONAL_SKILL_NAME_MAX_LENGTH}
              </small>
              {fieldErrors.name ? (
                <p className="field-error" id={nameErrorId} role="alert">
                  {fieldErrors.name}
                </p>
              ) : null}
            </div>

            <div className={FIELD_CLASS}>
              <label htmlFor={descriptionId}>技能描述</label>
              <textarea
                aria-describedby={errorDescription(
                  `${descriptionId}-help`,
                  fieldErrors.description ? descriptionErrorId : undefined,
                )}
                aria-invalid={fieldErrors.description ? true : undefined}
                className={FIELD_TEXTAREA_CLASS}
                disabled={fieldsDisabled}
                id={descriptionId}
                maxLength={PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH}
                onChange={(event) =>
                  updateField("description", event.target.value)
                }
                placeholder="说明该技能适用的任务与方法"
                rows={3}
                value={values.description}
              />
              <small id={`${descriptionId}-help`}>
                {Array.from(values.description).length} /{" "}
                {PERSONAL_SKILL_DESCRIPTION_MAX_LENGTH}
              </small>
              {fieldErrors.description ? (
                <p
                  className="field-error"
                  id={descriptionErrorId}
                  role="alert"
                >
                  {fieldErrors.description}
                </p>
              ) : null}
            </div>

            <div className={FIELD_CLASS}>
              <label htmlFor={guideId}>Markdown 正文</label>
              <MarkdownSplitEditor
                disabled={fieldsDisabled}
                error={fieldErrors.guide}
                id={guideId}
                importTriggerLabel="导入 .md"
                importingMarkdown={parseMutation.isPending}
                label="Markdown 正文"
                maxLength={PERSONAL_SKILL_GUIDE_MAX_LENGTH}
                minHeightClassName="min-h-[240px]"
                onChange={(nextGuide) => updateField("guide", nextGuide)}
                onImportMarkdownFile={(file) => void handleFileSelection(file)}
                placeholder={"# 工作方法\n\n1. 核对输入。\n2. 按步骤输出。"}
                rows={12}
                value={values.guide}
              />
            </div>
          </div>

          <div
            className={cn(
              NOTICE_BRAND_CLASS,
              "gap-[9px] rounded-[9px] px-3 py-[11px] text-brand [&>svg]:mt-px [&>svg]:shrink-0",
            )}
            role="note"
          >
            <Sparkle
              aria-hidden="true"
              focusable="false"
              size={16}
              strokeWidth={1.7}
            />
            <p>
              保存后会创建不可变版本并立即启用。已开始的任务继续使用原版本，新任务使用最新版本。
            </p>
          </div>

          {requestError ? (
            <div className="flex flex-col items-start gap-2">
              <InlineAlert size="compact" variant="danger">
                {requestError}
              </InlineAlert>
              {revisionConflict ? (
                <Button
                  onClick={() => void refreshLatest()}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <RefreshCw
                    aria-hidden="true"
                    focusable="false"
                    size={14}
                    strokeWidth={1.7}
                  />
                  刷新最新内容
                </Button>
              ) : null}
            </div>
          ) : null}
        </form>
      )}
    </Modal>
  );
}
