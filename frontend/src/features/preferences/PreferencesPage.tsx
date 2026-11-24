import {
  BookmarkPlus,
  Pencil,
  RotateCcw,
  Save,
  Sparkles,
  Trash2,
} from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { formatDateTime } from "../../shared/lib/format";
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
  Badge,
  Button,
  EmptyState,
  InlineAlert,
  Skeleton,
  Switch,
  Textarea,
  cn,
  toast,
} from "../../shared/ui";
import { preferenceErrorMessage } from "./errorMessages";
import {
  useCreateUserMemoryMutation,
  useDeleteUserMemoryMutation,
  useUpdateUserMemoryMutation,
  useUpdateUserPreferencesMutation,
  useUserMemories,
  useUserPreferences,
} from "./hooks";
import {
  CUSTOM_INSTRUCTIONS_MAX_LENGTH,
  USER_MEMORY_CONTENT_MAX_LENGTH,
  USER_MEMORY_DEFAULT_LIMIT,
  USER_MEMORY_WARNING_THRESHOLD,
  WORK_INSTRUCTION_PRESETS,
  applyInstructionPreset,
  countCodePoints,
  validateCustomInstructions,
  validateUserMemoryContent,
  type UserMemory,
} from "./model";
import { SettingsLayout } from "./SettingsLayout";

function memorySourceBadge(source: UserMemory["source"]) {
  if (source === "chat_explicit") {
    return <Badge variant="brand">对话记住</Badge>;
  }
  return <Badge variant="neutral">手动添加</Badge>;
}

interface DeleteMemoryDialogProps {
  memory: UserMemory | null;
  onClose: () => void;
}

/** 删除显式备忘二次确认弹窗：提交锁定并在异常时保留弹窗展示原因。 */
function DeleteMemoryDialog({ memory, onClose }: DeleteMemoryDialogProps) {
  const deleteMutation = useDeleteUserMemoryMutation();
  const { reset: resetMutation } = deleteMutation;
  const [requestError, setRequestError] = useState<string | null>(null);

  useEffect(() => {
    if (memory) {
      setRequestError(null);
      resetMutation();
    }
  }, [memory, resetMutation]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !deleteMutation.isPending) {
      onClose();
    }
  };

  const handleConfirm = async () => {
    if (!memory || deleteMutation.isPending) {
      return;
    }
    setRequestError(null);
    try {
      await deleteMutation.mutateAsync(memory.id);
      toast.success("显式备忘已删除");
      onClose();
    } catch (error) {
      setRequestError(preferenceErrorMessage(error));
    }
  };

  return (
    <AlertDialog onOpenChange={handleOpenChange} open={Boolean(memory)}>
      <AlertDialogContent closeDisabled={deleteMutation.isPending}>
        <AlertDialogHeader>
          <AlertDialogTitle>删除显式备忘</AlertDialogTitle>
          <AlertDialogDescription>
            删除后该条备忘将不再注入后续对话上下文，且无法从当前页面恢复。
          </AlertDialogDescription>
        </AlertDialogHeader>

        <AlertDialogBody className="space-y-3">
          {memory ? (
            <div className="rounded-lg border border-border-subtle bg-subtle px-3 py-2 text-sm text-text-primary">
              <p className="line-clamp-4 whitespace-pre-wrap break-words">
                {memory.content}
              </p>
            </div>
          ) : null}
          {requestError ? (
            <InlineAlert description={requestError} variant="danger" />
          ) : null}
        </AlertDialogBody>

        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleteMutation.isPending}>
            取消
          </AlertDialogCancel>
          {/* 阻止 Radix 默认自动关闭，确保异步提交完成或报错反馈后再决定是否关闭弹窗。 */}
          <AlertDialogAction
            loading={deleteMutation.isPending}
            onClick={(event) => {
              event.preventDefault();
              void handleConfirm();
            }}
            variant="danger"
          >
            确认删除
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

interface MemoryItemRowProps {
  memory: UserMemory;
  onRequestDelete: (memory: UserMemory) => void;
}

/** 单条显式备忘卡片：支持展示来源与更新时间、原地编辑及删除入口。 */
function MemoryItemRow({ memory, onRequestDelete }: MemoryItemRowProps) {
  const editTextareaId = useId();
  const editErrorId = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(memory.content);
  const [requestError, setRequestError] = useState<string | null>(null);
  const updateMutation = useUpdateUserMemoryMutation();

  const validationError = editing ? validateUserMemoryContent(draft) : null;
  const charCount = countCodePoints(draft.trim());
  const isOverLimit = charCount > USER_MEMORY_CONTENT_MAX_LENGTH;
  const isChanged = draft.trim() !== memory.content.trim();

  const startEdit = () => {
    setDraft(memory.content);
    setRequestError(null);
    updateMutation.reset();
    setEditing(true);
  };

  const cancelEdit = () => {
    if (updateMutation.isPending) {
      return;
    }
    setDraft(memory.content);
    setRequestError(null);
    setEditing(false);
  };

  const handleSave = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (updateMutation.isPending) {
      return;
    }
    const error = validateUserMemoryContent(draft);
    if (error) {
      return;
    }
    if (!isChanged) {
      setEditing(false);
      return;
    }
    setRequestError(null);
    try {
      await updateMutation.mutateAsync({
        input: { content: draft.trim() },
        memoryId: memory.id,
      });
      toast.success("备忘已更新");
      setEditing(false);
    } catch (error) {
      setRequestError(preferenceErrorMessage(error));
    }
  };

  if (editing) {
    return (
      <li className="rounded-xl border border-brand-border bg-brand-subtle/20 p-4">
        <form
          className="space-y-3"
          noValidate
          onSubmit={(event) => void handleSave(event)}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label
              className="text-xs font-medium text-text-secondary"
              htmlFor={editTextareaId}
            >
              编辑显式备忘
            </label>
            <div className="flex items-center gap-2">
              {memorySourceBadge(memory.source)}
              <span
                aria-live="polite"
                className={cn(
                  "font-mono text-xs tabular-nums",
                  isOverLimit ? "text-danger" : "text-text-tertiary",
                )}
              >
                {charCount} / {USER_MEMORY_CONTENT_MAX_LENGTH}
              </span>
            </div>
          </div>

          <Textarea
            aria-describedby={validationError ? editErrorId : undefined}
            aria-label="编辑备忘内容"
            disabled={updateMutation.isPending}
            error={Boolean(validationError)}
            id={editTextareaId}
            onChange={(event) => {
              setDraft(event.target.value);
              if (requestError) {
                setRequestError(null);
              }
            }}
            rows={3}
            showCount={false}
            value={draft}
          />

          {validationError ? (
            <p
              className="text-xs leading-snug text-danger"
              id={editErrorId}
              role="alert"
            >
              {validationError}
            </p>
          ) : null}

          {requestError ? (
            <InlineAlert description={requestError} variant="danger" />
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <Button
              disabled={updateMutation.isPending}
              onClick={cancelEdit}
              size="sm"
              type="button"
              variant="outline"
            >
              取消
            </Button>
            <Button
              disabled={Boolean(validationError)}
              loading={updateMutation.isPending}
              size="sm"
              type="submit"
              variant="primary"
            >
              保存
            </Button>
          </div>
        </form>
      </li>
    );
  }

  return (
    <li className="rounded-xl border border-border-subtle bg-canvas p-4 transition-colors hover:border-border-default">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {memorySourceBadge(memory.source)}
            <time
              className="text-xs text-text-tertiary"
              dateTime={memory.updated_at}
            >
              更新于 {formatDateTime(memory.updated_at)}
            </time>
          </div>
          <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-text-primary">
            {memory.content}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <Button
            aria-label={`编辑备忘：${memory.content.slice(0, 20)}`}
            onClick={startEdit}
            size="xs"
            type="button"
            variant="ghost"
          >
            <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
            编辑
          </Button>
          <Button
            aria-label={`删除备忘：${memory.content.slice(0, 20)}`}
            className="text-danger hover:bg-danger-bg hover:text-danger"
            onClick={() => onRequestDelete(memory)}
            size="xs"
            type="button"
            variant="ghost"
          >
            <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
            删除
          </Button>
        </div>
      </div>
    </li>
  );
}

/**
 * 个人工作偏好与显式记忆页面（/preferences）：
 * - 区块 A：长期工作指令（4,000 字码点校验、3 组办公预设芯片、脏草稿保护）；
 * - 区块 B：显式记忆总开关（乐观切换与失败回滚，严禁夹带未保存指令草稿）与显式备忘 CRUD。
 */
export function PreferencesPage() {
  const instructionsTextareaId = useId();
  const instructionsErrorId = useId();
  const memorySwitchId = useId();
  const newMemoryTextareaId = useId();
  const newMemoryErrorId = useId();

  const profile = useUserPreferences();
  const memories = useUserMemories();
  const updatePreferencesMutation = useUpdateUserPreferencesMutation();
  const createMemoryMutation = useCreateUserMemoryMutation();

  const [draftInstructions, setDraftInstructions] = useState("");
  const [initializedProfile, setInitializedProfile] = useState(false);
  // 记录上次与服务端同步的指令基线，后台刷新时仅在非脏态自动同步，绝不覆盖正在编辑的草稿。
  const syncedInstructionsBaselineRef = useRef<string | null>(null);
  const [instructionsRequestError, setInstructionsRequestError] = useState<
    string | null
  >(null);
  const [presetNotice, setPresetNotice] = useState<string | null>(null);
  const [pendingProfileAction, setPendingProfileAction] = useState<
    "instructions" | "toggle" | null
  >(null);

  const [newMemoryContent, setNewMemoryContent] = useState("");
  const [newMemoryTouched, setNewMemoryTouched] = useState(false);
  const [createMemoryError, setCreateMemoryError] = useState<string | null>(
    null,
  );
  const [deleteTarget, setDeleteTarget] = useState<UserMemory | null>(null);

  useEffect(() => {
    if (!profile.isSuccess) {
      return;
    }
    const serverInstructions = profile.data.custom_instructions;
    if (!initializedProfile) {
      setDraftInstructions(serverInstructions);
      syncedInstructionsBaselineRef.current = serverInstructions;
      setInitializedProfile(true);
      return;
    }
    // 若用户草稿与上次服务端基线一致（未编辑），则静默跟随服务端最新值；否则保留脏草稿。
    if (draftInstructions === syncedInstructionsBaselineRef.current) {
      setDraftInstructions(serverInstructions);
    }
    syncedInstructionsBaselineRef.current = serverInstructions;
  }, [
    draftInstructions,
    initializedProfile,
    profile.data,
    profile.isSuccess,
  ]);

  const savedInstructions = profile.data?.custom_instructions ?? "";
  const memoryEnabled = profile.data?.memory_enabled ?? true;
  const instructionsDirty =
    profile.isSuccess && draftInstructions !== savedInstructions;
  const instructionsValidationError =
    validateCustomInstructions(draftInstructions);
  const instructionsCount = countCodePoints(draftInstructions.trim());
  const instructionsOverLimit =
    instructionsCount > CUSTOM_INSTRUCTIONS_MAX_LENGTH;

  const memoryTotal = memories.data?.total ?? 0;
  const memoryLimit = memories.data?.limit ?? USER_MEMORY_DEFAULT_LIMIT;
  const isMemoryLimitReached =
    memories.isSuccess && memoryTotal >= memoryLimit;
  const isMemoryQuotaWarning =
    memories.isSuccess &&
    memoryTotal >= USER_MEMORY_WARNING_THRESHOLD &&
    memoryTotal < memoryLimit;
  const quotaRatioPercent = Math.min(
    100,
    memoryLimit > 0 ? Math.round((memoryTotal / memoryLimit) * 100) : 0,
  );

  const newMemoryTrimmedLength = countCodePoints(newMemoryContent.trim());
  const newMemoryOverLimit =
    newMemoryTrimmedLength > USER_MEMORY_CONTENT_MAX_LENGTH;
  const rawNewMemoryValidation = validateUserMemoryContent(newMemoryContent);
  // 仅在输入超限/含非法控制字符，或用户已尝试提交后展示校验错误，避免初始空框红字干扰。
  const showNewMemoryValidation =
    Boolean(rawNewMemoryValidation) &&
    (newMemoryTouched ||
      newMemoryOverLimit ||
      Boolean(newMemoryContent.length > 0 && newMemoryTrimmedLength > 0));
  const visibleNewMemoryError = showNewMemoryValidation
    ? rawNewMemoryValidation
    : null;

  const handleApplyPreset = (presetText: string) => {
    setPresetNotice(null);
    setInstructionsRequestError(null);
    const result = applyInstructionPreset(draftInstructions, presetText);
    if (result.exceeded) {
      setPresetNotice(
        `追加该预设将超过 ${CUSTOM_INSTRUCTIONS_MAX_LENGTH.toLocaleString()} 字上限，请先精简当前指令。`,
      );
      return;
    }
    setDraftInstructions(result.nextValue);
  };

  const handleResetInstructions = () => {
    setDraftInstructions(savedInstructions);
    setInstructionsRequestError(null);
    setPresetNotice(null);
  };

  const handleSaveInstructions = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (
      !profile.data ||
      !instructionsDirty ||
      instructionsValidationError ||
      updatePreferencesMutation.isPending
    ) {
      return;
    }
    setInstructionsRequestError(null);
    setPresetNotice(null);
    setPendingProfileAction("instructions");
    try {
      const normalized = draftInstructions.trim();
      const saved = await updatePreferencesMutation.mutateAsync({
        custom_instructions: normalized,
        memory_enabled: profile.data.memory_enabled,
      });
      setDraftInstructions(saved.custom_instructions);
      syncedInstructionsBaselineRef.current = saved.custom_instructions;
      toast.success("长期工作指令已保存");
    } catch (error) {
      setInstructionsRequestError(preferenceErrorMessage(error));
    } finally {
      setPendingProfileAction(null);
    }
  };

  const handleToggleMemoryEnabled = async (nextEnabled: boolean) => {
    if (!profile.data || updatePreferencesMutation.isPending) {
      return;
    }
    setPendingProfileAction("toggle");
    try {
      // 关键边界：切换显式记忆开关时，只提交服务端已保存的 custom_instructions，
      // 绝不夹带用户尚未点击「保存指令」的本地草稿。
      await updatePreferencesMutation.mutateAsync({
        custom_instructions: profile.data.custom_instructions,
        memory_enabled: nextEnabled,
      });
      toast.success(nextEnabled ? "已开启显式记忆" : "已关闭显式记忆");
    } catch (error) {
      toast.error(preferenceErrorMessage(error));
    } finally {
      setPendingProfileAction(null);
    }
  };

  const handleCreateMemory = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (isMemoryLimitReached || createMemoryMutation.isPending) {
      return;
    }
    setNewMemoryTouched(true);
    const validation = validateUserMemoryContent(newMemoryContent);
    if (validation) {
      return;
    }
    setCreateMemoryError(null);
    try {
      await createMemoryMutation.mutateAsync({
        content: newMemoryContent.trim(),
      });
      setNewMemoryContent("");
      setNewMemoryTouched(false);
      toast.success("已添加显式备忘");
    } catch (error) {
      setCreateMemoryError(preferenceErrorMessage(error));
    }
  };

  return (
    <SettingsLayout>
      <h2 className="sr-only">工作偏好与记忆</h2>

      {/* 区块 A：长期工作指令 */}
      <section
        aria-labelledby="work-instructions-title"
        className="rounded-xl border border-border-subtle bg-canvas p-5 shadow-xs sm:p-6"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <h3
              className="text-base font-semibold text-text-primary"
              id="work-instructions-title"
            >
              长期工作指令
            </h3>
            <p className="text-sm text-text-secondary">
              说明你的岗位角色、常用专业口径与回复风格，Athena 会在你的所有新对话中自动参考。
            </p>
          </div>
          {profile.data?.updated_at ? (
            <span className="text-xs text-text-tertiary">
              上次更新：{formatDateTime(profile.data.updated_at)}
            </span>
          ) : null}
        </div>

        {profile.isPending ? (
          <div className="mt-4 space-y-3" role="status">
            <span className="sr-only">正在加载个人工作偏好…</span>
            <Skeleton className="h-8 w-2/3" />
            <Skeleton className="h-32 w-full" />
          </div>
        ) : profile.isError ? (
          <div className="mt-4">
            <InlineAlert
              action={
                <Button
                  onClick={() => void profile.refetch()}
                  size="xs"
                  type="button"
                  variant="outline"
                >
                  重试
                </Button>
              }
              description={preferenceErrorMessage(profile.error)}
              title="无法加载工作偏好"
              variant="danger"
            />
          </div>
        ) : (
          <form
            className="mt-4 space-y-4"
            noValidate
            onSubmit={(event) => void handleSaveInstructions(event)}
          >
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-1.5 text-xs text-text-tertiary">
                <Sparkles aria-hidden="true" className="h-3.5 w-3.5 text-brand" />
                <span>参考指令预设（点击填入或换行追加）：</span>
              </div>
              <div className="flex flex-wrap gap-2">
                {WORK_INSTRUCTION_PRESETS.map((preset) => (
                  <button
                    className={cn(
                      "inline-flex items-center rounded-full border border-border-default bg-subtle px-3 py-1 text-xs font-medium text-text-secondary transition-colors",
                      "hover:border-brand-border hover:bg-brand-subtle hover:text-brand",
                      "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40",
                      "disabled:pointer-events-none disabled:opacity-50",
                    )}
                    disabled={pendingProfileAction === "instructions"}
                    key={preset.id}
                    onClick={() => handleApplyPreset(preset.text)}
                    type="button"
                  >
                    + {preset.label}
                  </button>
                ))}
              </div>
            </div>

            {presetNotice ? (
              <InlineAlert description={presetNotice} variant="warning" />
            ) : null}

            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <label
                  className="text-xs font-medium text-text-secondary"
                  htmlFor={instructionsTextareaId}
                >
                  工作背景与输出偏好
                </label>
                <span
                  aria-live="polite"
                  className={cn(
                    "font-mono text-xs tabular-nums",
                    instructionsOverLimit
                      ? "font-medium text-danger"
                      : instructionsCount >= CUSTOM_INSTRUCTIONS_MAX_LENGTH - 200
                        ? "text-warning"
                        : "text-text-tertiary",
                  )}
                >
                  {instructionsCount} / {CUSTOM_INSTRUCTIONS_MAX_LENGTH}
                </span>
              </div>

              <Textarea
                aria-describedby={
                  instructionsValidationError ? instructionsErrorId : undefined
                }
                disabled={pendingProfileAction === "instructions"}
                error={Boolean(instructionsValidationError)}
                id={instructionsTextareaId}
                onChange={(event) => {
                  setDraftInstructions(event.target.value);
                  setPresetNotice(null);
                  if (instructionsRequestError) {
                    setInstructionsRequestError(null);
                  }
                }}
                placeholder="例如：我是财务分析专员，回复时请先给结论再列制度依据，金额统一按万元展示。"
                rows={5}
                showCount={false}
                value={draftInstructions}
              />

              {instructionsValidationError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={instructionsErrorId}
                  role="alert"
                >
                  {instructionsValidationError}
                </p>
              ) : (
                <p className="text-xs text-text-tertiary">
                  清空内容后点击保存即可移除个人长期指令；单条指令上限 4,000 字。
                </p>
              )}
            </div>

            {instructionsRequestError ? (
              <InlineAlert
                description={instructionsRequestError}
                variant="danger"
              />
            ) : null}

            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border-subtle pt-4">
              <span className="text-xs text-text-tertiary">
                {instructionsDirty ? "有未保存的草稿更改" : "当前内容已与服务端同步"}
              </span>
              <div className="flex items-center gap-2">
                <Button
                  disabled={
                    !instructionsDirty || pendingProfileAction === "instructions"
                  }
                  onClick={handleResetInstructions}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
                  撤销修改
                </Button>
                <Button
                  disabled={
                    !instructionsDirty ||
                    Boolean(instructionsValidationError) ||
                    updatePreferencesMutation.isPending
                  }
                  loading={pendingProfileAction === "instructions"}
                  size="sm"
                  type="submit"
                  variant="primary"
                >
                  <Save aria-hidden="true" className="h-3.5 w-3.5" />
                  保存指令
                </Button>
              </div>
            </div>
          </form>
        )}
      </section>

      {/* 区块 B：显式记忆总开关 + 显式备忘列表 */}
      <section
        aria-labelledby="explicit-memories-title"
        className="rounded-xl border border-border-subtle bg-canvas p-5 shadow-xs sm:p-6"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="space-y-1">
            <h3
              className="text-base font-semibold text-text-primary"
              id="explicit-memories-title"
            >
              显式记忆与个人备忘
            </h3>
            <p className="text-sm text-text-secondary">
              在这里管理你手动添加或在对话中要求 Athena 记住的关键事实，随时可编辑或删除。
            </p>
          </div>

          <div className="flex items-center gap-3 rounded-lg border border-border-subtle bg-subtle px-3 py-2">
            <label
              className="cursor-pointer text-xs font-medium text-text-primary select-none"
              htmlFor={memorySwitchId}
            >
              启用显式记忆
            </label>
            <Switch
              aria-label="启用显式记忆"
              checked={memoryEnabled}
              disabled={!profile.isSuccess || updatePreferencesMutation.isPending}
              id={memorySwitchId}
              onCheckedChange={(checked) => void handleToggleMemoryEnabled(checked)}
            />
          </div>
        </div>

        {profile.isSuccess && !memoryEnabled ? (
          <div className="mt-4">
            <InlineAlert
              description="已关闭对话自动记忆与注入，历史备忘仍保存在下方，重新开启后将在后续对话中恢复生效。"
              variant="info"
            />
          </div>
        ) : null}

        {/* 容量配额条 */}
        <div className="mt-5 rounded-lg border border-border-subtle bg-subtle/70 p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="font-medium text-text-primary">
              {memories.isPending
                ? "正在读取显式备忘配额…"
                : memories.isError
                  ? "显式备忘配额暂不可用"
                  : `已保存 ${memoryTotal} / ${memoryLimit} 条`}
            </span>
            {isMemoryLimitReached ? (
              <Badge variant="danger">已达上限</Badge>
            ) : isMemoryQuotaWarning ? (
              <Badge variant="warning">接近上限，建议精简</Badge>
            ) : null}
          </div>
          <div
            aria-label="显式备忘容量使用率"
            aria-valuemax={memoryLimit}
            aria-valuemin={0}
            aria-valuenow={memories.isSuccess ? memoryTotal : 0}
            className="mt-2 h-2 w-full overflow-hidden rounded-full bg-border-subtle"
            role="progressbar"
          >
            <div
              className={cn(
                "h-full rounded-full transition-all duration-200",
                isMemoryLimitReached
                  ? "bg-danger"
                  : isMemoryQuotaWarning
                    ? "bg-warning"
                    : "bg-brand",
              )}
              style={{ width: `${quotaRatioPercent}%` }}
            />
          </div>
          {isMemoryLimitReached ? (
            <p className="mt-2 text-xs text-danger">
              显式备忘已达 {memoryLimit} 条上限，请先删除下方旧备忘后再添加新内容。
            </p>
          ) : isMemoryQuotaWarning ? (
            <p className="mt-2 text-xs text-warning">
              当前已使用 {memoryTotal} / {memoryLimit} 条，接近配额上限，可适时精简过时备忘。
            </p>
          ) : null}
        </div>

        {/* 快速新增表单 */}
        <form
          className="mt-5 space-y-3 rounded-xl border border-border-subtle bg-subtle/40 p-4"
          noValidate
          onSubmit={(event) => void handleCreateMemory(event)}
        >
          <div className="flex items-center justify-between gap-2">
            <label
              className="text-xs font-medium text-text-secondary"
              htmlFor={newMemoryTextareaId}
            >
              手动添加显式备忘
            </label>
            <span
              aria-live="polite"
              className={cn(
                "font-mono text-xs tabular-nums",
                newMemoryOverLimit ? "font-medium text-danger" : "text-text-tertiary",
              )}
            >
              {newMemoryTrimmedLength} / {USER_MEMORY_CONTENT_MAX_LENGTH}
            </span>
          </div>

          <Textarea
            aria-describedby={
              visibleNewMemoryError ? newMemoryErrorId : undefined
            }
            disabled={isMemoryLimitReached || createMemoryMutation.isPending}
            error={Boolean(visibleNewMemoryError)}
            id={newMemoryTextareaId}
            onChange={(event) => {
              setNewMemoryContent(event.target.value);
              if (createMemoryError) {
                setCreateMemoryError(null);
              }
            }}
            placeholder={
              isMemoryLimitReached
                ? "显式备忘已达上限，请先删除旧备忘后再添加"
                : "例如：周报金额统一使用万元为单位，并标注同比环比"
            }
            rows={3}
            showCount={false}
            value={newMemoryContent}
          />

          {visibleNewMemoryError ? (
            <p
              className="text-xs leading-snug text-danger"
              id={newMemoryErrorId}
              role="alert"
            >
              {visibleNewMemoryError}
            </p>
          ) : null}

          {createMemoryError ? (
            <InlineAlert description={createMemoryError} variant="danger" />
          ) : null}

          <div className="flex justify-end">
            <Button
              disabled={
                isMemoryLimitReached ||
                !newMemoryContent.trim() ||
                Boolean(rawNewMemoryValidation)
              }
              loading={createMemoryMutation.isPending}
              size="sm"
              type="submit"
              variant="primary"
            >
              <BookmarkPlus aria-hidden="true" className="h-3.5 w-3.5" />
              添加备忘
            </Button>
          </div>
        </form>

        {/* 记忆列表 */}
        <div className="mt-5">
          {memories.isPending ? (
            <div className="space-y-3" role="status">
              <span className="sr-only">正在加载显式备忘列表…</span>
              <Skeleton className="h-20 w-full rounded-xl" />
              <Skeleton className="h-20 w-full rounded-xl" />
            </div>
          ) : memories.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void memories.refetch()}
                  size="xs"
                  type="button"
                  variant="outline"
                >
                  重试
                </Button>
              }
              description={preferenceErrorMessage(memories.error)}
              title="显式备忘加载失败"
              variant="danger"
            />
          ) : memories.data.items.length === 0 ? (
            <EmptyState
              compact
              description="你可以在上方手动添加常用工作事实，或在对话中直接告诉 Athena「记住这件事」。"
              icon="spark"
              title="还没有显式备忘"
            />
          ) : (
            <ul aria-label="显式备忘列表" className="space-y-3">
              {memories.data.items.map((memory) => (
                <MemoryItemRow
                  key={memory.id}
                  memory={memory}
                  onRequestDelete={setDeleteTarget}
                />
              ))}
            </ul>
          )}
        </div>
      </section>

      <DeleteMemoryDialog
        memory={deleteTarget}
        onClose={() => setDeleteTarget(null)}
      />
    </SettingsLayout>
  );
}
