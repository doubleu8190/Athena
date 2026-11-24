import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router-dom";

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
  Checkbox,
  InlineAlert,
  toast,
} from "../../shared/ui";
import { useSessionList } from "../sessions/hooks";
import { projectErrorMessage } from "./errorMessages";
import { useDeleteProjectMutation } from "./hooks";
import type { Project } from "./model";

interface DeleteProjectDialogProps {
  onClose: () => void;
  /** 可选成功回调；缺省时自动跳转回 /projects。 */
  onDeleted?: () => void;
  open: boolean;
  project: Project;
}

/**
 * 温和删除项目弹窗（Radix AlertDialog）：
 * - 默认勾选「保留历史对话」，提交 DELETE ?detach_sessions=true 将历史对话转为普通对话；
 * - 仅当存在历史对话且用户主动取消勾选时提示会被服务端拒绝；
 * - 遇 409 session_has_active_run / project_has_sessions 在弹窗内通过 InlineAlert 明确反馈且不关闭。
 */
export function DeleteProjectDialog({
  onClose,
  onDeleted,
  open,
  project,
}: DeleteProjectDialogProps) {
  const navigate = useNavigate();
  const detachCheckboxId = useId();
  const [detachSessions, setDetachSessions] = useState(true);
  const [requestError, setRequestError] = useState<string | null>(null);
  const sessions = useSessionList(
    { page: 1, pageSize: 1, projectId: project.id },
    open,
  );
  const mutation = useDeleteProjectMutation(project.id);
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setDetachSessions(true);
      setRequestError(null);
      resetMutation();
    }
  }, [open, project.id, resetMutation]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !mutation.isPending) {
      onClose();
    }
  };

  const sessionCount = sessions.data?.total ?? 0;
  const showStrictDeleteWarning =
    sessions.isSuccess && sessionCount > 0 && !detachSessions;

  const handleConfirmDelete = async () => {
    if (mutation.isPending) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync({ detachSessions });
      onClose();
      toast.success(
        detachSessions
          ? "项目已删除，历史对话已保留为普通对话"
          : "项目已删除",
      );
      if (onDeleted) {
        onDeleted();
      } else {
        navigate("/projects", { replace: true });
      }
    } catch (failure) {
      setRequestError(projectErrorMessage(failure));
    }
  };

  return (
    <AlertDialog onOpenChange={handleOpenChange} open={open}>
      <AlertDialogContent closeDisabled={mutation.isPending}>
        <AlertDialogHeader>
          <AlertDialogTitle>删除项目</AlertDialogTitle>
          <AlertDialogDescription>
            确定删除「{project.name}」吗？删除后该项目文件夹将从列表与侧栏中移除。
          </AlertDialogDescription>
        </AlertDialogHeader>

        <AlertDialogBody className="space-y-3">
          <div className="flex items-start gap-2.5 rounded-lg border border-border-subtle bg-subtle px-3 py-2.5">
            <Checkbox
              checked={detachSessions}
              className="mt-0.5"
              disabled={mutation.isPending}
              id={detachCheckboxId}
              onCheckedChange={(checked) => {
                setDetachSessions(checked === true);
                if (requestError) {
                  setRequestError(null);
                }
              }}
            />
            <label
              className="cursor-pointer text-xs leading-relaxed text-text-primary select-none"
              htmlFor={detachCheckboxId}
            >
              保留历史对话（推荐，将项目内历史对话转为普通对话，不丢失记录）
            </label>
          </div>

          {showStrictDeleteWarning ? (
            <InlineAlert
              description={`该项目下仍有 ${sessionCount} 个历史对话。取消勾选「保留历史对话」后直接删除会被服务端拒绝，建议保持勾选或先清理对话。`}
              title="直接删除将会被拦截"
              variant="warning"
            />
          ) : null}

          {requestError ? (
            <InlineAlert description={requestError} variant="danger" />
          ) : null}
        </AlertDialogBody>

        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>
            取消
          </AlertDialogCancel>
          {/* 阻止 Radix 自动关闭，以便在 409 session_has_active_run / project_has_sessions 时保留弹窗展示错误。 */}
          <AlertDialogAction
            loading={mutation.isPending}
            onClick={(event) => {
              event.preventDefault();
              void handleConfirmDelete();
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
