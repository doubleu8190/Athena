import { useEffect, useId, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { projectErrorMessage } from "../../features/projects/errorMessages";
import { useProjectDirectory } from "../../features/projects/hooks";
import { sessionErrorMessage } from "../../features/sessions/errorMessages";
import {
  useDeleteAnySessionMutation,
  useUpdateSessionMutation,
} from "../../features/sessions/hooks";
import { sessionDisplayTitle, type Session } from "../../features/sessions/model";
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
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  InlineAlert,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  toast,
} from "../../shared/ui";
import { activeSessionIdFromPath } from "./navigation";

// Radix Select 不允许空字符串作为 SelectItem 的 value，使用前缀编码将真实项目 ID 与「未归属项目」哨兵彻底隔离。
const UNBOUND_PROJECT_VALUE = "__unbound__";
const PROJECT_VALUE_PREFIX = "project:";

function toProjectSelectValue(projectId: string | null): string {
  return projectId ? `${PROJECT_VALUE_PREFIX}${projectId}` : UNBOUND_PROJECT_VALUE;
}

function fromProjectSelectValue(value: string): string | null {
  if (value === UNBOUND_PROJECT_VALUE) {
    return null;
  }
  return value.startsWith(PROJECT_VALUE_PREFIX)
    ? value.slice(PROJECT_VALUE_PREFIX.length)
    : value;
}

interface MoveSessionProjectDialogProps {
  onClose: () => void;
  session: Session | null;
}

/** 侧栏「移动至项目 / 移出项目」弹窗：支持闲置会话（含已有历史消息）随时调整项目归属。 */
export function MoveSessionProjectDialog({
  onClose,
  session,
}: MoveSessionProjectDialogProps) {
  const selectId = useId();
  const projects = useProjectDirectory();
  const updateMutation = useUpdateSessionMutation();
  const { reset: resetMutation } = updateMutation;
  const [selectedValue, setSelectedValue] = useState(UNBOUND_PROJECT_VALUE);
  const [requestError, setRequestError] = useState<string | null>(null);

  const open = Boolean(session);

  useEffect(() => {
    if (session) {
      setSelectedValue(toProjectSelectValue(session.project_id));
      setRequestError(null);
      resetMutation();
    }
  }, [resetMutation, session]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !updateMutation.isPending) {
      onClose();
    }
  };

  const handleConfirm = async () => {
    if (!session || updateMutation.isPending) {
      return;
    }
    const nextProjectId = fromProjectSelectValue(selectedValue);

    // 归属未发生变化时直接收起弹窗，避免无意义写请求。
    if (nextProjectId === session.project_id) {
      onClose();
      return;
    }

    setRequestError(null);
    try {
      await updateMutation.mutateAsync({
        input: { project_id: nextProjectId },
        sessionId: session.id,
      });
      const targetProject = (projects.data ?? []).find(
        (item) => item.id === nextProjectId,
      );
      toast.success(
        targetProject
          ? `已移动至「${targetProject.name}」`
          : "已移出项目，设为未归属对话",
      );
      onClose();
    } catch (error) {
      setRequestError(sessionErrorMessage(error));
    }
  };

  const projectItems = projects.data ?? [];
  const hasCurrentProjectInList =
    !session?.project_id ||
    projectItems.some((item) => item.id === session.project_id);

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent closeDisabled={updateMutation.isPending} size="sm">
        <DialogHeader>
          <DialogTitle>调整所属项目</DialogTitle>
          <DialogDescription>
            选择目标项目或移出至未归属项目；当前无执行中任务的对话均可随时调整。
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {session ? (
            <div className="rounded-lg border border-border-subtle bg-subtle px-3 py-2">
              <div className="text-xs text-text-tertiary">目标对话</div>
              <div className="mt-0.5 truncate text-sm font-medium text-text-primary">
                {sessionDisplayTitle(session.title)}
              </div>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <label
              className="block text-xs font-medium text-text-secondary"
              htmlFor={selectId}
            >
              目标项目
            </label>
            {projects.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : projects.isError ? (
              <InlineAlert
                action={
                  <Button
                    onClick={() => void projects.refetch()}
                    size="xs"
                    variant="outline"
                  >
                    重试
                  </Button>
                }
                description={projectErrorMessage(projects.error)}
                title="项目列表加载失败"
                variant="danger"
              />
            ) : (
              <Select
                disabled={updateMutation.isPending}
                onValueChange={(value) => {
                  setSelectedValue(value);
                  setRequestError(null);
                }}
                value={selectedValue}
              >
                <SelectTrigger aria-label="目标项目" id={selectId}>
                  <SelectValue placeholder="请选择目标项目" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={UNBOUND_PROJECT_VALUE}>
                    未归属项目
                  </SelectItem>
                  {projectItems.map((project) => (
                    <SelectItem
                      key={project.id}
                      value={toProjectSelectValue(project.id)}
                    >
                      {project.name}
                    </SelectItem>
                  ))}
                  {!hasCurrentProjectInList && session?.project_id ? (
                    <SelectItem
                      disabled
                      value={toProjectSelectValue(session.project_id)}
                    >
                      当前项目（不可访问）
                    </SelectItem>
                  ) : null}
                </SelectContent>
              </Select>
            )}
          </div>

          {requestError ? (
            <InlineAlert description={requestError} variant="danger" />
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button
            disabled={updateMutation.isPending}
            onClick={onClose}
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={projects.isPending || projects.isError}
            loading={updateMutation.isPending}
            onClick={() => void handleConfirm()}
            variant="primary"
          >
            保存归属
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface DeleteSessionAlertDialogProps {
  onClose: () => void;
  session: Session | null;
}

/** 侧栏危险删除二次确认弹窗：删除当前所在路由会话后立即回跳 /chat。 */
export function DeleteSessionAlertDialog({
  onClose,
  session,
}: DeleteSessionAlertDialogProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const deleteMutation = useDeleteAnySessionMutation();
  const { reset: resetMutation } = deleteMutation;
  const [requestError, setRequestError] = useState<string | null>(null);

  const open = Boolean(session);

  useEffect(() => {
    if (session) {
      setRequestError(null);
      resetMutation();
    }
  }, [resetMutation, session]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !deleteMutation.isPending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    if (!session || deleteMutation.isPending) {
      return;
    }
    setRequestError(null);
    try {
      await deleteMutation.mutateAsync(session.id);
      const isDeletingCurrentRoute =
        activeSessionIdFromPath(location.pathname) === session.id;
      onClose();
      toast.success("已删除对话");
      if (isDeletingCurrentRoute) {
        navigate("/chat", { replace: true });
      }
    } catch (error) {
      setRequestError(sessionErrorMessage(error));
    }
  };

  return (
    <AlertDialog onOpenChange={handleOpenChange} open={open}>
      <AlertDialogContent closeDisabled={deleteMutation.isPending}>
        <AlertDialogHeader>
          <AlertDialogTitle>删除对话</AlertDialogTitle>
          <AlertDialogDescription>
            确定删除「{session ? sessionDisplayTitle(session.title) : ""}
            」吗？删除后该对话将从历史记录中移除，无法从此页面恢复。
          </AlertDialogDescription>
        </AlertDialogHeader>

        {requestError ? (
          <AlertDialogBody>
            <InlineAlert description={requestError} variant="danger" />
          </AlertDialogBody>
        ) : null}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleteMutation.isPending}>
            取消
          </AlertDialogCancel>
          {/* 阻止 Radix Action 默认自动关闭，确保提交态锁定并在 409 错误时保留弹窗展示原因。 */}
          <AlertDialogAction
            loading={deleteMutation.isPending}
            onClick={(event) => {
              event.preventDefault();
              void handleDelete();
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
