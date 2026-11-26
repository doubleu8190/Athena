import { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  InlineAlert,
  Input,
  cn,
  toast,
} from "../../shared/ui";
import { projectErrorMessage } from "./errorMessages";
import {
  useCreateProjectMutation,
  useUpdateProjectMutation,
} from "./hooks";
import {
  PROJECT_DESCRIPTION_MAX_LENGTH,
  countProjectCodePoints,
  normalizeProjectDescription,
  normalizeProjectName,
  validateProjectDescription,
  validateProjectName,
  type Project,
  type ProjectUpdateInput,
} from "./model";

interface NewProjectDialogProps {
  onClose: () => void;
  open: boolean;
}

/** 新建项目弹窗：支持填写项目名称与可选 500 字内简介，创建成功后直接进入单页工作文件夹。 */
export function NewProjectDialog({ onClose, open }: NewProjectDialogProps) {
  const navigate = useNavigate();
  const formId = useId();
  const nameErrorId = useId();
  const descErrorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  // 受控 Dialog 未包裹 DialogTrigger 时，显式记录打开前焦点并在关闭后归还。
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [descError, setDescError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateProjectMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setNameError(null);
      setDescError(null);
      setRequestError(null);
      resetMutation();
    }
    // 每次打开都从干净表单开始，不复用上一次的失败状态。
  }, [open, resetMutation]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !mutation.isPending) {
      onClose();
    }
  };

  const descLength = countProjectCodePoints(description.trim());
  const descOverLimit = descLength > PROJECT_DESCRIPTION_MAX_LENGTH;

  const handleSubmit = async () => {
    const nextNameError = validateProjectName(name);
    const nextDescError = validateProjectDescription(description);
    setNameError(nextNameError);
    setDescError(nextDescError);
    if (nextNameError) {
      inputRef.current?.focus();
      return;
    }
    if (nextDescError) {
      return;
    }

    setRequestError(null);
    const normalizedDescription = normalizeProjectDescription(description);
    try {
      const project = await mutation.mutateAsync({
        name: normalizeProjectName(name),
        ...(normalizedDescription !== null
          ? { description: normalizedDescription }
          : {}),
      });
      // 创建成功后即将跳转到新项目页，清空归还目标避免把焦点拉回旧触发钮。
      previousFocusRef.current = null;
      onClose();
      toast.success("项目已创建");
      navigate(`/projects/${encodeURIComponent(project.id)}`);
    } catch (requestFailure) {
      setRequestError(projectErrorMessage(requestFailure));
    }
  };

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent
        closeDisabled={mutation.isPending}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const target = previousFocusRef.current;
          previousFocusRef.current = null;
          if (target?.isConnected) {
            target.focus();
          }
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          previousFocusRef.current =
            document.activeElement instanceof HTMLElement
              ? document.activeElement
              : null;
          inputRef.current?.focus();
        }}
        size="sm"
      >
        <DialogHeader>
          <DialogTitle>新建项目</DialogTitle>
          <DialogDescription>
            项目文件夹用于组织同一主题下的对话、专属指令、默认技能与关联知识库。
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          <form
            className="space-y-4"
            id={formId}
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void handleSubmit();
            }}
          >
            <div className="space-y-1.5">
              <label
                className="block text-xs font-medium text-text-secondary"
                htmlFor={`${formId}-name`}
              >
                项目名称
              </label>
              <Input
                aria-describedby={nameError ? nameErrorId : undefined}
                aria-label="项目名称"
                autoComplete="off"
                disabled={mutation.isPending}
                error={Boolean(nameError)}
                id={`${formId}-name`}
                onChange={(event) => {
                  setName(event.target.value);
                  if (nameError) setNameError(null);
                  if (requestError) setRequestError(null);
                }}
                placeholder="例如：Q4 财务经营分析"
                ref={inputRef}
                value={name}
              />
              <p className="text-xs text-text-tertiary">
                1–120 个 Unicode 码点，不能包含控制字符。
              </p>
              {nameError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={nameErrorId}
                  role="alert"
                >
                  {nameError}
                </p>
              ) : null}
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <label
                  className="block text-xs font-medium text-text-secondary"
                  htmlFor={`${formId}-description`}
                >
                  项目简介（可选）
                </label>
                <span
                  aria-live="polite"
                  className={cn(
                    "font-mono text-xs tabular-nums",
                    descOverLimit ? "text-danger" : "text-text-tertiary",
                  )}
                >
                  {descLength} / {PROJECT_DESCRIPTION_MAX_LENGTH}
                </span>
              </div>
              <Input
                aria-describedby={descError ? descErrorId : undefined}
                aria-label="项目简介"
                autoComplete="off"
                disabled={mutation.isPending}
                error={Boolean(descError)}
                id={`${formId}-description`}
                onChange={(event) => {
                  setDescription(event.target.value);
                  if (descError) setDescError(null);
                  if (requestError) setRequestError(null);
                }}
                placeholder="简要说明这个工作文件夹的主题或用途（500 字内）"
                value={description}
              />
              {descError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={descErrorId}
                  role="alert"
                >
                  {descError}
                </p>
              ) : null}
            </div>

            {requestError ? (
              <InlineAlert description={requestError} variant="danger" />
            ) : null}
          </form>
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={mutation.isPending}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            form={formId}
            loading={mutation.isPending}
            type="submit"
            variant="primary"
          >
            创建项目
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface RenameProjectDialogProps {
  onClose: () => void;
  open: boolean;
  project: Project;
}

/** 重命名项目弹窗：复用统一 Radix Dialog 原语，供项目列表菜单快捷修改名称。 */
export function RenameProjectDialog({
  onClose,
  open,
  project,
}: RenameProjectDialogProps) {
  const formId = useId();
  const errorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [name, setName] = useState(project.name);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUpdateProjectMutation(project.id);
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setName(project.name);
      setValidationError(null);
      setRequestError(null);
      resetMutation();
    }
  }, [open, project.name, resetMutation]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const error = validateProjectName(name);
    if (error) {
      setValidationError(error);
      inputRef.current?.focus();
      return;
    }

    setValidationError(null);
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        name: normalizeProjectName(name),
      });
      toast.success("项目名称已更新");
      onClose();
    } catch (requestFailure) {
      setRequestError(projectErrorMessage(requestFailure));
    }
  };

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent
        closeDisabled={mutation.isPending}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const target = previousFocusRef.current;
          previousFocusRef.current = null;
          if (target?.isConnected) {
            target.focus();
          }
        }}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          previousFocusRef.current =
            document.activeElement instanceof HTMLElement
              ? document.activeElement
              : null;
          inputRef.current?.focus();
          inputRef.current?.select();
        }}
        size="sm"
      >
        <DialogHeader>
          <DialogTitle>重命名项目</DialogTitle>
          <DialogDescription>
            修改后将同步更新侧栏与项目中心中的显示名称。
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          <form
            className="space-y-3"
            id={formId}
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void handleSubmit();
            }}
          >
            <div className="space-y-1.5">
              <label
                className="block text-xs font-medium text-text-secondary"
                htmlFor={`${formId}-name`}
              >
                项目名称
              </label>
              <Input
                aria-describedby={validationError ? errorId : undefined}
                aria-label="项目名称"
                autoComplete="off"
                disabled={mutation.isPending}
                error={Boolean(validationError)}
                id={`${formId}-name`}
                onChange={(event) => {
                  setName(event.target.value);
                  setValidationError(null);
                  setRequestError(null);
                }}
                ref={inputRef}
                value={name}
              />
              <p className="text-xs text-text-tertiary">
                1–120 个 Unicode 码点，不能包含控制字符。
              </p>
              {validationError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={errorId}
                  role="alert"
                >
                  {validationError}
                </p>
              ) : null}
            </div>

            {requestError ? (
              <InlineAlert description={requestError} variant="danger" />
            ) : null}
          </form>
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={mutation.isPending}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            form={formId}
            loading={mutation.isPending}
            type="submit"
            variant="primary"
          >
            保存名称
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface EditProjectInfoDialogProps {
  onClose: () => void;
  open: boolean;
  project: Project;
}

/** 编辑项目基本信息弹窗：同时修改项目名称与简介，支持清空简介并显式发送 description: null。 */
export function EditProjectInfoDialog({
  onClose,
  open,
  project,
}: EditProjectInfoDialogProps) {
  const formId = useId();
  const nameErrorId = useId();
  const descErrorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const [nameError, setNameError] = useState<string | null>(null);
  const [descError, setDescError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUpdateProjectMutation(project.id);
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setName(project.name);
      setDescription(project.description ?? "");
      setNameError(null);
      setDescError(null);
      setRequestError(null);
      resetMutation();
    }
  }, [open, project.description, project.name, resetMutation]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !mutation.isPending) {
      onClose();
    }
  };

  const descLength = countProjectCodePoints(description.trim());
  const descOverLimit = descLength > PROJECT_DESCRIPTION_MAX_LENGTH;

  const handleSubmit = async () => {
    const nextNameError = validateProjectName(name);
    const nextDescError = validateProjectDescription(description);
    setNameError(nextNameError);
    setDescError(nextDescError);
    if (nextNameError) {
      inputRef.current?.focus();
      return;
    }
    if (nextDescError) {
      return;
    }

    const normalizedName = normalizeProjectName(name);
    const normalizedDescription = normalizeProjectDescription(description);
    const currentDescription = project.description;

    const patch: ProjectUpdateInput = {};
    if (normalizedName !== project.name) {
      patch.name = normalizedName;
    }
    if (normalizedDescription !== currentDescription) {
      // 清空简介时显式传 null，符合后端 PATCH 局部清空契约。
      patch.description = normalizedDescription;
    }

    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }

    setRequestError(null);
    try {
      await mutation.mutateAsync(patch);
      toast.success("项目信息已更新");
      onClose();
    } catch (requestFailure) {
      setRequestError(projectErrorMessage(requestFailure));
    }
  };

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent closeDisabled={mutation.isPending} size="sm">
        <DialogHeader>
          <DialogTitle>编辑项目信息</DialogTitle>
          <DialogDescription>
            修改项目名称或简介；将简介留空并保存可清空当前简介。
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          <form
            className="space-y-4"
            id={formId}
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void handleSubmit();
            }}
          >
            <div className="space-y-1.5">
              <label
                className="block text-xs font-medium text-text-secondary"
                htmlFor={`${formId}-edit-name`}
              >
                项目名称
              </label>
              <Input
                aria-describedby={nameError ? nameErrorId : undefined}
                aria-label="项目名称"
                autoComplete="off"
                disabled={mutation.isPending}
                error={Boolean(nameError)}
                id={`${formId}-edit-name`}
                onChange={(event) => {
                  setName(event.target.value);
                  if (nameError) setNameError(null);
                  if (requestError) setRequestError(null);
                }}
                ref={inputRef}
                value={name}
              />
              {nameError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={nameErrorId}
                  role="alert"
                >
                  {nameError}
                </p>
              ) : null}
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <label
                  className="block text-xs font-medium text-text-secondary"
                  htmlFor={`${formId}-edit-description`}
                >
                  项目简介
                </label>
                <span
                  aria-live="polite"
                  className={cn(
                    "font-mono text-xs tabular-nums",
                    descOverLimit ? "text-danger" : "text-text-tertiary",
                  )}
                >
                  {descLength} / {PROJECT_DESCRIPTION_MAX_LENGTH}
                </span>
              </div>
              <Input
                aria-describedby={descError ? descErrorId : undefined}
                aria-label="项目简介"
                autoComplete="off"
                disabled={mutation.isPending}
                error={Boolean(descError)}
                id={`${formId}-edit-description`}
                onChange={(event) => {
                  setDescription(event.target.value);
                  if (descError) setDescError(null);
                  if (requestError) setRequestError(null);
                }}
                placeholder="填写简要说明，留空保存可清空"
                value={description}
              />
              {descError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={descErrorId}
                  role="alert"
                >
                  {descError}
                </p>
              ) : null}
            </div>

            {requestError ? (
              <InlineAlert description={requestError} variant="danger" />
            ) : null}
          </form>
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={mutation.isPending}
            onClick={onClose}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            form={formId}
            loading={mutation.isPending}
            type="submit"
            variant="primary"
          >
            保存信息
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
