import { useEffect, useId, useRef, useState } from "react";
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
  const projectOptions = [
    { id: "", name: "未归属项目" },
    ...allProjects.map((project) => ({ id: project.id, name: project.name })),
  ];
  if (
    selectedProjectQuery.data &&
    !allProjects.some((project) => project.id === selectedProjectQuery.data?.id)
  ) {
    projectOptions.push({
      id: selectedProjectQuery.data.id,
      name: selectedProjectQuery.data.name,
    });
  }

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
        <div className="chat-context-select">
          <span>更改项目</span>
          <ProjectDropdown
            disabled={!contextReady || projects.isPending}
            id={selectId}
            onChange={(projectId) => chooseProject(projectId || null)}
            options={projectOptions}
            value={selectedProjectId ?? ""}
          />
        </div>
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

interface ProjectDropdownProps {
  disabled: boolean;
  id: string;
  onChange: (projectId: string) => void;
  options: Array<{ id: string; name: string }>;
  value: string;
}

function ProjectDropdown({
  disabled,
  id,
  onChange,
  options,
  value,
}: ProjectDropdownProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.id === value),
  );
  const [activeProjectId, setActiveProjectId] = useState(
    () => options[selectedIndex]?.id ?? "",
  );
  const effectiveActiveProjectId =
    options.find((option) => option.id === activeProjectId)?.id ??
    options[selectedIndex]?.id ??
    options[0]?.id ??
    "";
  const effectiveActiveIndex = Math.max(
    0,
    options.findIndex((option) => option.id === effectiveActiveProjectId),
  );
  const selected = options[selectedIndex];
  const activeOptionId = `${id}-option-${effectiveActiveIndex}`;

  useEffect(() => {
    if (!options.some((option) => option.id === activeProjectId)) {
      // 目录刷新移除了当前项时，回到已选项目，避免 Enter 落到无效项目。
      setActiveProjectId(options[selectedIndex]?.id ?? options[0]?.id ?? "");
    }
  }, [activeProjectId, options, selectedIndex]);

  useEffect(() => {
    if (disabled && open) setOpen(false);
  }, [disabled, open]);

  useEffect(() => {
    if (!open) return;
    const activeOption = rootRef.current?.querySelector<HTMLElement>(
      `[id="${activeOptionId}"]`,
    );
    activeOption?.scrollIntoView?.({ block: "nearest" });
  }, [activeOptionId, open]);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    // 只在点击控件外部时收起菜单，内部选择仍由选项自身处理。
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  const openAt = (index: number) => {
    const boundedIndex = Math.min(options.length - 1, Math.max(0, index));
    setActiveProjectId(options[boundedIndex]?.id ?? options[selectedIndex]?.id ?? "");
    setOpen(true);
  };

  return (
    <div className="chat-project-dropdown" ref={rootRef}>
      <button
        aria-activedescendant={open ? activeOptionId : undefined}
        aria-controls={`${id}-listbox`}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label="更改项目"
        className="chat-project-dropdown-trigger"
        disabled={disabled}
        id={id}
        role="combobox"
        onClick={() => (open ? setOpen(false) : openAt(selectedIndex))}
        onBlur={(event) => {
          if (!rootRef.current?.contains(event.relatedTarget as Node | null)) {
            setOpen(false);
          }
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            openAt(open ? effectiveActiveIndex + 1 : selectedIndex);
          } else if (event.key === "ArrowUp") {
            event.preventDefault();
            openAt(open ? effectiveActiveIndex - 1 : selectedIndex);
          } else if (event.key === "Home" && open) {
            event.preventDefault();
            setActiveProjectId(options[0]?.id ?? "");
          } else if (event.key === "End" && open) {
            event.preventDefault();
            setActiveProjectId(options[options.length - 1]?.id ?? "");
          } else if ((event.key === "Enter" || event.key === " ") && open) {
            event.preventDefault();
            onChange(effectiveActiveProjectId);
            setOpen(false);
          } else if (event.key === "Escape" && open) {
            event.preventDefault();
            setOpen(false);
          }
        }}
        type="button"
      >
        <span>{selected?.name ?? "未归属项目"}</span>
        <Icon className="chat-project-dropdown-chevron" name="chevron" size={12} />
      </button>
      {open ? (
        <div
          aria-label="项目"
          className="chat-project-dropdown-menu"
          id={`${id}-listbox`}
          role="listbox"
        >
          {options.map((option, index) => (
            <button
              aria-selected={option.id === value}
              className="chat-project-dropdown-option"
              data-active={option.id === effectiveActiveProjectId}
              id={`${id}-option-${index}`}
              key={option.id || "unassigned"}
              onClick={() => {
                onChange(option.id);
                setOpen(false);
              }}
              onMouseDown={(event) => {
                // 保持焦点在触发器，选择后仍可继续使用键盘操作。
                event.preventDefault();
              }}
              onMouseMove={() => setActiveProjectId(option.id)}
              role="option"
              tabIndex={-1}
              type="button"
            >
              <span className="chat-project-dropdown-option-label">{option.name}</span>
              {option.id === value ? (
                <span aria-hidden="true" className="chat-project-dropdown-check">
                  ✓
                </span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
