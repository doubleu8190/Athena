import { useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import { knowledgeErrorMessage } from "../knowledge/errorMessages";
import {
  useBindProjectKnowledgeMutation,
  useKnowledgeDirectory,
  useProjectKnowledgeBases,
  useUnbindProjectKnowledgeMutation,
} from "../knowledge/hooks";
import {
  PROJECT_KNOWLEDGE_LIMIT,
  type KnowledgeBase,
} from "../knowledge/model";
import type { Project } from "./model";

interface AssociateKnowledgeDialogProps {
  boundIds: Set<string>;
  onClose: () => void;
  open: boolean;
  projectId: string;
}

function AssociateKnowledgeDialog({
  boundIds,
  onClose,
  open,
  projectId,
}: AssociateKnowledgeDialogProps) {
  const bases = useKnowledgeDirectory(open);
  const bind = useBindProjectKnowledgeMutation(projectId);
  const [requestError, setRequestError] = useState<string | null>(null);
  const limitReached = boundIds.size >= PROJECT_KNOWLEDGE_LIMIT;
  const available =
    bases.data?.filter((base) => !boundIds.has(base.id)) ?? [];

  useEffect(() => {
    if (open) {
      setRequestError(null);
      bind.reset();
    }
  }, [open]);

  const handleBind = async (baseId: string) => {
    setRequestError(null);
    try {
      await bind.mutateAsync(baseId);
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={bind.isPending}
      description={`每个项目最多关联 ${PROJECT_KNOWLEDGE_LIMIT} 个知识库。`}
      footer={
        <button className="button-quiet" onClick={onClose} type="button">
          关闭
        </button>
      }
      onClose={onClose}
      open={open}
      panelClassName="knowledge-associate-dialog"
      title="关联知识库"
    >
      {limitReached ? (
        <p className="knowledge-form-status" role="status">
          该项目已关联 {PROJECT_KNOWLEDGE_LIMIT} 个知识库。请先解除一个已有知识库。
        </p>
      ) : null}
      {bases.isPending ? (
        <p className="knowledge-form-status" role="status">
          正在加载知识库目录…
        </p>
      ) : bases.isError ? (
        <ErrorBanner
          message={knowledgeErrorMessage(bases.error)}
          onRetry={() => void bases.refetch()}
        />
      ) : available.length === 0 ? (
        <EmptyState
          compact
          action={
            <Link className="button-quiet" to="/knowledge">
              前往知识库
            </Link>
          }
          description="没有可关联的新知识库，可先创建或解除其他知识库。"
          icon="book"
          title="暂无可关联知识库"
        />
      ) : (
        <div className="knowledge-associate-list">
          {available.map((base) => (
            <article className="knowledge-associate-row" key={base.id}>
              <div>
                <strong>{base.name}</strong>
                <span>{base.description || "未填写说明"}</span>
              </div>
              <button
                className="button-quiet"
                disabled={limitReached || bind.isPending}
                onClick={() => void handleBind(base.id)}
                type="button"
              >
                {bind.isPending && bind.variables === base.id
                  ? "正在关联…"
                  : "关联"}
              </button>
            </article>
          ))}
        </div>
      )}
      {requestError ? <ErrorBanner message={requestError} /> : null}
    </Modal>
  );
}

interface UnbindKnowledgeDialogProps {
  base: KnowledgeBase;
  onClose: () => void;
  onUnbound: () => void;
  open: boolean;
  projectId: string;
}

function UnbindKnowledgeDialog({
  base,
  onClose,
  onUnbound,
  open,
  projectId,
}: UnbindKnowledgeDialogProps) {
  const checkboxId = useId();
  const [confirmed, setConfirmed] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUnbindProjectKnowledgeMutation(projectId);

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      mutation.reset();
    }
  }, [open]);

  const handleUnbind = async () => {
    if (!confirmed) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync(base.id);
      onClose();
      onUnbound();
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="知识库本身不会被删除。"
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
            onClick={() => void handleUnbind()}
            type="button"
          >
            {mutation.isPending ? "正在解除…" : "解除关联"}
          </button>
        </>
      }
      onClose={onClose}
      open={open}
      title="解除知识库关联"
    >
      <p className="confirm-copy">
        确定将“{base.name}”从当前项目解除关联吗？
      </p>
      <label className="workspace-checkbox" htmlFor={checkboxId}>
        <input
          checked={confirmed}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我已确认要解除关联</span>
      </label>
      {requestError ? <ErrorBanner message={requestError} /> : null}
    </Modal>
  );
}

interface ProjectKnowledgeTabProps {
  project: Project;
}

export function ProjectKnowledgeTab({ project }: ProjectKnowledgeTabProps) {
  const bases = useProjectKnowledgeBases(project.id);
  const [associateOpen, setAssociateOpen] = useState(false);
  const [unbindBase, setUnbindBase] = useState<KnowledgeBase | null>(null);
  const boundIds = new Set(
    bases.isSuccess ? bases.data.map((base) => base.id) : [],
  );
  const limitReached =
    bases.isSuccess &&
    bases.data.length >= PROJECT_KNOWLEDGE_LIMIT;

  useEffect(() => {
    if (bases.isError) {
      setAssociateOpen(false);
    }
  }, [bases.isError]);

  return (
    <div className="project-tab-content">
      <header className="project-panel-heading">
        <div>
          <p className="page-eyebrow">Project knowledge</p>
          <h2>项目知识库</h2>
          <p>项目只使用当前账号拥有的真实知识库。</p>
        </div>
        <button
          aria-describedby={
            limitReached ? "project-knowledge-limit" : undefined
          }
          className="button-primary"
          disabled={!bases.isSuccess || limitReached}
          onClick={() => setAssociateOpen(true)}
          type="button"
        >
          <Icon name="plus" size={14} />
          关联知识库
        </button>
      </header>

      <div className="knowledge-project-summary">
        {bases.isPending ? (
          <span role="status">已关联数量读取中</span>
        ) : bases.isError ? (
          <span role="status">已关联数量暂不可用</span>
        ) : (
          <span>
            已关联 {bases.data.length} / {PROJECT_KNOWLEDGE_LIMIT}
          </span>
        )}
        {limitReached ? (
          <span id="project-knowledge-limit">
            已达到项目关联上限，请先解除一个知识库。
          </span>
        ) : null}
      </div>

      {bases.isPending ? (
        <div className="page-loading" role="status">
          正在加载项目知识库…
        </div>
      ) : bases.isError ? (
        <ErrorBanner
          message={knowledgeErrorMessage(bases.error)}
          onRetry={() => void bases.refetch()}
        />
      ) : bases.data.length === 0 ? (
        <EmptyState
          action={
            <button
              className="button-primary"
              onClick={() => setAssociateOpen(true)}
              type="button"
            >
              关联第一个知识库
            </button>
          }
          description="关联后，项目中的对话可以使用这些知识库作为检索上下文。"
          icon="book"
          title="还没有关联知识库"
        />
      ) : (
        <ul className="knowledge-project-grid">
          {bases.data.map((base) => (
            <li className="knowledge-project-card" key={base.id}>
              <div>
                <p className="knowledge-card-kicker">Knowledge base</p>
                <Link
                  className="knowledge-card-title"
                  to={`/knowledge/${encodeURIComponent(base.id)}`}
                >
                  <strong>{base.name}</strong>
                  <Icon name="arrow-right" size={16} />
                </Link>
                <p className="knowledge-card-description">
                  {base.description || "未填写说明"}
                </p>
              </div>
              <p className="knowledge-project-time">
                更新于 {formatDateTime(base.updated_at)}
              </p>
              <button
                aria-label={`解除知识库关联：${base.name}`}
                className="button-danger-outline knowledge-card-action"
                onClick={() => setUnbindBase(base)}
                type="button"
              >
                解除关联
              </button>
            </li>
          ))}
        </ul>
      )}

      <AssociateKnowledgeDialog
        boundIds={boundIds}
        onClose={() => setAssociateOpen(false)}
        open={associateOpen}
        projectId={project.id}
      />
      {unbindBase ? (
        <UnbindKnowledgeDialog
          base={unbindBase}
          onClose={() => setUnbindBase(null)}
          onUnbound={() => setUnbindBase(null)}
          open
          projectId={project.id}
        />
      ) : null}
    </div>
  );
}
