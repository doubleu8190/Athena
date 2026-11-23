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
} from "../../shared/ui";
import { projectErrorMessage } from "./errorMessages";
import {
  useCreateProjectMutation,
  useRenameProjectMutation,
} from "./hooks";
import {
  normalizeProjectName,
  type Project,
  validateProjectName,
} from "./model";

interface NewProjectDialogProps {
  onClose: () => void;
  open: boolean;
}

/** 新建项目弹窗：采用设计系统统一 Radix Dialog，确保在移动端 Sheet 抽屉内嵌套打开时共享 FocusScope 与 DismissableLayer。 */
export function NewProjectDialog({
  onClose,
  open,
}: NewProjectDialogProps) {
  const navigate = useNavigate();
  const formId = useId();
  const errorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  // 受控 Dialog 未包裹 DialogTrigger 时，显式记录打开前焦点并在关闭后归还（如抽屉内的「新建项目」按钮）。
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const [name, setName] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateProjectMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setName("");
      setValidationError(null);
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
      const project = await mutation.mutateAsync({
        name: normalizeProjectName(name),
      });
      // 创建成功后即将跳转到新项目页，清空归还目标避免把焦点拉回旧触发钮。
      previousFocusRef.current = null;
      onClose();
      navigate(`/projects/${encodeURIComponent(project.id)}/overview`);
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
            项目用于组织同一主题下的对话。名称可在创建后修改。
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          <form
            className="space-y-3"
            id={formId}
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
                  if (validationError) {
                    setValidationError(null);
                  }
                  if (requestError) {
                    setRequestError(null);
                  }
                }}
                placeholder="例如：产品研究"
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

/** 重命名项目弹窗：复用统一 Radix Dialog 原语。 */
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
  const mutation = useRenameProjectMutation(project.id);
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
