import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import { useSessionList } from "../sessions/hooks";
import { projectErrorMessage } from "./errorMessages";
import {
  useDeleteProjectMutation,
} from "./hooks";
import type { Project } from "./model";
import { RenameProjectDialog } from "./ProjectNameDialogs";

interface DeleteProjectDialogProps {
  onClose: () => void;
  open: boolean;
  project: Project;
}

function DeleteProjectDialog({
  onClose,
  open,
  project,
}: DeleteProjectDialogProps) {
  const navigate = useNavigate();
  const checkboxId = useId();
  const [confirmed, setConfirmed] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useDeleteProjectMutation(project.id);

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      mutation.reset();
    }
  }, [open]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    if (!confirmed) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync();
      onClose();
      navigate("/projects", { replace: true });
    } catch (failure) {
      setRequestError(projectErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="此操作会从项目中心移除该项目。"
      footer={
        <>
          <button
            className="button-quiet"
            data-autofocus
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-danger"
            disabled={!confirmed || mutation.isPending}
            onClick={() => void handleDelete()}
            type="button"
          >
            {mutation.isPending ? "正在删除…" : "删除项目"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="删除项目"
    >
      <p className="confirm-copy">
        确定删除“{project.name}”吗？删除后无法在项目中心继续访问。
      </p>
      <label
        className="workspace-checkbox workspace-checkbox--danger"
        htmlFor={checkboxId}
      >
        <input
          checked={confirmed}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我已确认要删除这个项目</span>
      </label>
      {requestError ? <ErrorBanner message={requestError} /> : null}
    </Modal>
  );
}

interface ProjectSettingsTabProps {
  project: Project;
}

export function ProjectSettingsTab({ project }: ProjectSettingsTabProps) {
  const sessions = useSessionList({
    page: 1,
    pageSize: 1,
    projectId: project.id,
  });
  const [renameOpen, setRenameOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const sessionsTotal = sessions.data?.total;
  const deleteBlocked = (sessionsTotal ?? 0) > 0;
  const deleteReason = deleteBlocked
    ? `该项目仍有 ${sessionsTotal} 个会话。请先在“会话”页删除全部会话。`
    : sessions.isError
      ? "暂时无法确认会话数量；继续删除时仍会由服务端校验。"
      : null;

  return (
    <div className="project-tab-content">
      <section className="surface-card settings-card">
        <div className="surface-header">
          <div className="surface-title">
            <strong>项目名称</strong>
            <span>名称会同步显示在对话侧栏和最近记录中</span>
          </div>
          <button
            className="button-quiet"
            onClick={() => setRenameOpen(true)}
            type="button"
          >
            <Icon name="edit" size={14} />
            重命名
          </button>
        </div>
        <p className="settings-project-name">{project.name}</p>
        <dl className="settings-metadata">
          <div>
            <dt>创建时间</dt>
            <dd>{formatDateTime(project.created_at)}</dd>
          </div>
          <div>
            <dt>更新时间</dt>
            <dd>{formatDateTime(project.updated_at)}</dd>
          </div>
        </dl>
      </section>

      <section className="surface-card danger-zone">
        <div className="surface-title">
          <strong>删除项目</strong>
          <span>项目只有在没有有效会话时才能删除</span>
        </div>
        <p>
          删除会从项目中心移除该项目。若服务端发现仍有会话，会拒绝本次操作并保留项目。
        </p>
        {deleteReason ? (
          <p className="settings-delete-reason" id="project-delete-reason">
            {deleteReason}
          </p>
        ) : null}
        <button
          aria-describedby={
            deleteReason ? "project-delete-reason" : undefined
          }
          className="button-danger-outline"
          disabled={deleteBlocked || sessions.isPending}
          onClick={() => setDeleteOpen(true)}
          type="button"
        >
          <Icon name="trash" size={14} />
          删除项目
        </button>
      </section>

      <RenameProjectDialog
        onClose={() => setRenameOpen(false)}
        open={renameOpen}
        project={project}
      />
      <DeleteProjectDialog
        onClose={() => setDeleteOpen(false)}
        open={deleteOpen}
        project={project}
      />
    </div>
  );
}

