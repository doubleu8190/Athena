import { useId, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { projectErrorMessage } from "../projects/errorMessages";
import { useProject, useProjectDirectory } from "../projects/hooks";
import { useCreateSessionMutation } from "../sessions/hooks";
import { submitRun } from "./api";
import { useDraftChat } from "./DraftChatContext";
import {
  chatErrorMessage,
  requestIdCanBeReused,
} from "./errorMessages";
import { newRequestId } from "./requestId";

export function DraftChatPage() {
  const selectId = useId();
  const navigate = useNavigate();
  const projects = useProjectDirectory();
  const createSession = useCreateSessionMutation();
  const {
    contextNotice,
    contextReady,
    draft,
    projectId: selectedProjectId,
    chooseProject,
    setSessionHandoff,
    setDraft,
  } = useDraftChat();
  const selectedProjectQuery = useProject(selectedProjectId);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const allProjects = projects.data ?? [];
  const selectedProject = allProjects.find(
    (project) => project.id === selectedProjectId,
  );
  const selectedProjectName =
    selectedProject?.name ??
    (selectedProjectQuery.data?.id === selectedProjectId
      ? selectedProjectQuery.data.name
      : null);

  const handleSubmit = async () => {
    const text = draft.trim();
    if (!text || submitting || !contextReady) {
      return;
    }
    if (Array.from(text).length > 32_000) {
      setSubmitError("消息内容不能超过 32,000 个字符。");
      return;
    }

    setSubmitError(null);
    setSubmitting(true);
    let createdSessionId: string;
    try {
      // 新草稿直到用户真正发送才创建 Session；标题保持为空，由后端展示“未命名对话”。
      const session = await createSession.mutateAsync({
        project_id: selectedProjectId,
        title: null,
      });
      createdSessionId = session.id;
    } catch (error) {
      setSubmitError(chatErrorMessage(error));
      setSubmitting(false);
      return;
    }

    const requestId = newRequestId();
    try {
      await submitRun(createdSessionId, requestId, text);
      setDraft("");
      setSubmitting(false);
      navigate(`/chat/${encodeURIComponent(createdSessionId)}`);
    } catch (error) {
      // Run 结果不明时不再停留在草稿页，避免用户再次发送时重复创建 Session。
      setSessionHandoff(createdSessionId, {
        draft: text,
        error: chatErrorMessage(error),
        requestId,
        reuseRequestId: requestIdCanBeReused(error),
      });
      setDraft("");
      setSubmitting(false);
      navigate(`/chat/${encodeURIComponent(createdSessionId)}`);
    }
  };

  return (
    <div className="page-shell chat-draft-page">
      <header className="page-heading chat-page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Workspace / New conversation</p>
          <h1>对话工作台</h1>
          <p className="page-description">
            这是本地新对话草稿。选择项目只会保存在当前页面；首次发送时才创建真实会话并提交
            Run。
          </p>
        </div>
        <span className="integration-chip">
          <span aria-hidden="true" />
          阶段 3
        </span>
      </header>

      <section className="chat-context-card" aria-labelledby="draft-context-title">
        <div className="chat-context-copy">
          <span className="chat-context-label" id="draft-context-title">
            当前项目上下文
          </span>
          <strong>
            {!contextReady
              ? "正在确认项目…"
              : selectedProjectName ?? "未归属项目"}
          </strong>
          <span>
            {selectedProjectId
              ? "首条消息发送前仍可更改绑定。"
              : "这次对话不会归入任何项目。"}
          </span>
        </div>
        <label className="chat-context-select" htmlFor={selectId}>
          <span>更改项目</span>
          <select
            disabled={!contextReady || projects.isPending}
            id={selectId}
            onChange={(event) => {
              chooseProject(event.target.value || null);
            }}
            value={selectedProjectId ?? ""}
          >
            <option value="">未归属项目</option>
            {allProjects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
            {selectedProjectQuery.data &&
            !allProjects.some(
              (project) => project.id === selectedProjectQuery.data?.id,
            ) ? (
              <option value={selectedProjectQuery.data.id}>
                {selectedProjectQuery.data.name}
              </option>
            ) : null}
          </select>
        </label>
      </section>

      {contextNotice ? (
        <div className="context-notice" role="status">
          <Icon name="inbox" size={16} />
          <span>{contextNotice}</span>
        </div>
      ) : null}

      {projects.isError && !selectedProjectQuery.data ? (
        <ErrorBanner
          message={projectErrorMessage(projects.error)}
          onRetry={() => void projects.refetch()}
        />
      ) : null}

      <section className="chat-empty-stage" aria-labelledby="draft-stage-title">
        <span className="chat-stage-mark" aria-hidden="true">
          <Icon name="spark" size={28} />
        </span>
        <p className="empty-panel-kicker">Ready to run</p>
        <h2 id="draft-stage-title">从一个清晰的目标开始</h2>
        <p>
          消息发送后会创建真实 Session 并提交 Run。运行中会展示服务端返回的状态、临时输出和工具审批请求。
        </p>
      </section>

      <form
        className="draft-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label htmlFor="draft-message">对话草稿</label>
        <textarea
          disabled={submitting}
          id="draft-message"
          onChange={(event) => setDraft(event.target.value)}
          placeholder="描述目标、粘贴资料，或直接提问。"
          rows={4}
          value={draft}
        />
        {submitError ? <ErrorBanner message={submitError} /> : null}
        <div className="draft-composer-footer">
          <p id="draft-send-note">
            <Icon name="shield" size={13} />
            发送时才创建会话；Run 提交使用幂等请求标识。
          </p>
          <button
            aria-describedby="draft-send-note"
            className="button-primary"
            disabled={!draft.trim() || submitting || !contextReady}
            type="submit"
          >
            {submitting ? "正在提交…" : "发送消息"}
          </button>
        </div>
      </form>
    </div>
  );
}
