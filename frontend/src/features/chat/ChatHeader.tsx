import {
  Folder,
  FolderInput,
  Loader2,
  Lock,
  MoreHorizontal,
  Paperclip,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";

import {
  DeleteSessionAlertDialog,
  MoveSessionProjectDialog,
} from "../../app/shell/SessionActionDialogs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Skeleton,
  Tooltip,
  toast,
} from "../../shared/ui";
import { useSessionFiles } from "../files/hooks";
import { useProjectDirectory } from "../projects/hooks";
import { sessionErrorMessage } from "../sessions/errorMessages";
import { useUpdateSessionMutation } from "../sessions/hooks";
import {
  SESSION_TITLE_MAX_LENGTH,
  normalizeSessionTitle,
  sessionDisplayTitle,
  validateSessionTitle,
  type Session,
} from "../sessions/model";

export interface ChatHeaderProps {
  hasActiveRun: boolean;
  onOpenFilesDrawer: () => void;
  session: Session | null;
  sessionId: string;
}

/**
 * 48px 极简单行会话顶栏：
 * - 左侧：支持内联点击编辑的会话标题（严格防 IME 误触）+ 所属项目胶囊（闲置可切换归属、运行中锁定并提示）；
 * - 右侧：[📎 N] 会话文件抽屉触发钮 + 紧凑 `...` 更多菜单（重命名、调整所属项目、删除对话）。
 * 包裹 React.memo 隔离流式 text_delta 高频更新时的无关重渲染。
 */
export const ChatHeader = memo(function ChatHeader({
  hasActiveRun,
  onOpenFilesDrawer,
  session,
  sessionId,
}: ChatHeaderProps) {
  const projectsQuery = useProjectDirectory();
  const filesQuery = useSessionFiles(sessionId, Boolean(sessionId));
  const updateSessionMutation = useUpdateSessionMutation();

  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState(session?.title ?? "");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [focusRetryToken, setFocusRetryToken] = useState(0);
  const [moveDialogOpen, setMoveDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);
  const cancelGuardRef = useRef(false);
  const submittingRef = useRef(false);
  const lastFailedDraftRef = useRef<string | null>(null);
  const pendingRenameFocusRef = useRef(false);
  // 仅在编辑态 false -> true 切入时初始化草稿，避免后台自动标题刷新或侧栏更新覆盖用户正在输入的文本。
  const latestTitleRef = useRef<string | null>(session?.title ?? null);
  latestTitleRef.current = session?.title ?? null;
  const wasEditingRef = useRef(false);

  useEffect(() => {
    // 切换会话时重置顶栏局部编辑态与错误提示。
    setIsEditingTitle(false);
    setInlineError(null);
    setMoveDialogOpen(false);
    setDeleteDialogOpen(false);
    wasEditingRef.current = false;
  }, [sessionId]);

  useEffect(() => {
    const enteredEditing = isEditingTitle && !wasEditingRef.current;
    wasEditingRef.current = isEditingTitle;
    if (!enteredEditing) {
      return;
    }
    setDraftTitle(latestTitleRef.current ?? "");
    setInlineError(null);
    cancelGuardRef.current = false;
    submittingRef.current = false;
    lastFailedDraftRef.current = null;
    const frameId = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [isEditingTitle]);

  useEffect(() => {
    if (
      !isEditingTitle ||
      focusRetryToken === 0 ||
      updateSessionMutation.isPending
    ) {
      return;
    }
    const frameId = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [focusRetryToken, isEditingTitle, updateSessionMutation.isPending]);

  const fileCount = useMemo(() => {
    const flattened = (filesQuery.data?.pages ?? []).flatMap(
      (page) => page.items,
    );
    return new Set(flattened.map((file) => file.id)).size;
  }, [filesQuery.data?.pages]);

  const projectName = useMemo(() => {
    if (!session) {
      return null;
    }
    if (!session.project_id) {
      return "未归属项目";
    }
    if (projectsQuery.isPending) {
      return "加载项目…";
    }
    return (
      (projectsQuery.data ?? []).find((item) => item.id === session.project_id)
        ?.name ?? "项目不可访问"
    );
  }, [projectsQuery.data, projectsQuery.isPending, session]);

  const cancelRename = () => {
    cancelGuardRef.current = true;
    submittingRef.current = false;
    lastFailedDraftRef.current = null;
    setInlineError(null);
    setDraftTitle(latestTitleRef.current ?? "");
    setIsEditingTitle(false);
  };

  const commitRename = async (trigger: "blur" | "enter") => {
    if (!session || cancelGuardRef.current || submittingRef.current) {
      return;
    }
    if (trigger === "blur" && lastFailedDraftRef.current === draftTitle) {
      return;
    }

    const validationError = validateSessionTitle(draftTitle);
    if (validationError) {
      setInlineError(validationError);
      return;
    }

    const normalizedNext = normalizeSessionTitle(draftTitle);
    const normalizedCurrent = latestTitleRef.current?.trim() ?? "";
    if (normalizedCurrent && normalizedNext === normalizedCurrent) {
      setInlineError(null);
      setIsEditingTitle(false);
      return;
    }

    submittingRef.current = true;
    setInlineError(null);
    try {
      await updateSessionMutation.mutateAsync({
        input: { title: normalizedNext },
        sessionId: session.id,
      });
      lastFailedDraftRef.current = null;
      toast.success("已更新对话标题");
      setIsEditingTitle(false);
    } catch (error) {
      submittingRef.current = false;
      lastFailedDraftRef.current = draftTitle;
      setInlineError(sessionErrorMessage(error));
      setFocusRetryToken((token) => token + 1);
    }
  };

  const displayTitle = session ? sessionDisplayTitle(session.title) : null;

  return (
    <>
      <header className="relative flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border-subtle bg-canvas px-4 sm:px-6">
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          {!session ? (
            <div className="flex items-center gap-2" role="status">
              <Skeleton className="h-5 w-40" />
              <span className="sr-only">正在加载会话…</span>
            </div>
          ) : isEditingTitle ? (
            <div className="relative flex min-w-0 items-center gap-1.5">
              <div className="flex h-8 w-56 items-center gap-1 rounded-lg border border-ring-focus bg-canvas px-2 shadow-xs ring-2 ring-ring-focus/15 sm:w-72">
                <input
                  aria-invalid={Boolean(inlineError) || undefined}
                  aria-label="编辑会话标题"
                  autoComplete="off"
                  className="min-w-0 flex-1 border-0 bg-transparent text-sm font-medium text-text-primary placeholder:text-text-placeholder focus-visible:outline-hidden"
                  disabled={updateSessionMutation.isPending}
                  maxLength={SESSION_TITLE_MAX_LENGTH}
                  onBlur={() => {
                    void commitRename("blur");
                  }}
                  onChange={(event) => {
                    setDraftTitle(event.target.value);
                    lastFailedDraftRef.current = null;
                    if (inlineError) {
                      setInlineError(null);
                    }
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      event.preventDefault();
                      event.stopPropagation();
                      cancelRename();
                      return;
                    }
                    // 严格拦截中文/日文输入法组合态（含 Safari/WebKit keyCode === 229），防止选字回车误提交。
                    if (
                      event.key === "Enter" &&
                      !event.nativeEvent.isComposing &&
                      event.nativeEvent.keyCode !== 229
                    ) {
                      event.preventDefault();
                      void commitRename("enter");
                    }
                  }}
                  placeholder="输入对话标题"
                  ref={inputRef}
                  type="text"
                  value={draftTitle}
                />
                {updateSessionMutation.isPending ? (
                  <Loader2
                    aria-hidden="true"
                    className="h-3.5 w-3.5 shrink-0 animate-spin text-text-tertiary motion-reduce:animate-none"
                  />
                ) : (
                  <button
                    aria-label="取消编辑标题"
                    className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-text-tertiary transition-colors hover:bg-muted hover:text-text-primary"
                    data-compact-control="true"
                    onClick={cancelRename}
                    onMouseDown={(event) => {
                      // 阻止点击取消按钮时先触发输入框 blur 提交。
                      event.preventDefault();
                    }}
                    type="button"
                  >
                    <X aria-hidden="true" className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              {inlineError ? (
                <p
                  className="absolute top-full left-0 z-20 mt-1 rounded-md border border-danger-border bg-danger-bg px-2.5 py-1 text-xs whitespace-nowrap text-danger shadow-xs"
                  role="alert"
                >
                  {inlineError}
                </p>
              ) : null}
            </div>
          ) : (
            <h1 className="min-w-0 truncate text-sm font-semibold tracking-tight text-text-primary transition-opacity duration-200">
              <button
                className="group/title inline-flex max-w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                data-compact-control="true"
                onClick={() => setIsEditingTitle(true)}
                title="点击重命名对话"
                type="button"
              >
                <span className="truncate">{displayTitle}</span>
                <Pencil
                  aria-hidden="true"
                  className="h-3 w-3 shrink-0 text-text-tertiary opacity-0 transition-opacity group-hover/title:opacity-100 group-focus-visible/title:opacity-100"
                />
              </button>
            </h1>
          )}

          {session && projectName ? (
            hasActiveRun ? (
              <Tooltip content="任务进行中，暂不可更改所属项目">
                <button
                  aria-disabled="true"
                  aria-label={`所属项目：${projectName}（任务进行中不可更改）`}
                  className="inline-flex h-6 max-w-[160px] shrink-0 cursor-not-allowed items-center gap-1 rounded-full border border-border-subtle bg-subtle px-2.5 text-xs font-medium text-text-tertiary opacity-80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                  data-compact-control="true"
                  onClick={(event) => event.preventDefault()}
                  type="button"
                >
                  <Lock aria-hidden="true" className="h-3 w-3 shrink-0" />
                  <span className="truncate">{projectName}</span>
                </button>
              </Tooltip>
            ) : (
              <Tooltip content="点击调整所属项目">
                <button
                  aria-label={`所属项目：${projectName}`}
                  className="inline-flex h-6 max-w-[160px] shrink-0 items-center gap-1 rounded-full border border-border-default bg-subtle px-2.5 text-xs font-medium text-text-secondary transition-colors hover:border-brand-border hover:bg-brand-subtle/70 hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                  data-compact-control="true"
                  onClick={() => setMoveDialogOpen(true)}
                  type="button"
                >
                  <Folder aria-hidden="true" className="h-3 w-3 shrink-0" />
                  <span className="truncate">{projectName}</span>
                </button>
              </Tooltip>
            )
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <Tooltip content="查看本对话上传文件与生成产物">
            <button
              aria-label={`会话文件（${fileCount}）`}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border-default bg-canvas px-2.5 text-xs font-medium text-text-secondary transition-colors hover:border-border-strong hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={onOpenFilesDrawer}
              type="button"
            >
              <Paperclip aria-hidden="true" className="h-3.5 w-3.5" />
              <span className="tabular-nums">{fileCount}</span>
            </button>
          </Tooltip>

          {session ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  aria-label="更多对话操作"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                  data-compact-control="true"
                  type="button"
                >
                  <MoreHorizontal aria-hidden="true" className="h-4 w-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                onCloseAutoFocus={(event) => {
                  // 菜单触发进入内联编辑时阻止 Radix 将焦点抢回触发按钮，避免立刻触发输入框 blur。
                  if (pendingRenameFocusRef.current) {
                    event.preventDefault();
                    pendingRenameFocusRef.current = false;
                  }
                }}
              >
                <DropdownMenuItem
                  onSelect={() => {
                    pendingRenameFocusRef.current = true;
                    setIsEditingTitle(true);
                  }}
                >
                  <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>重命名对话</span>
                </DropdownMenuItem>
                <DropdownMenuItem
                  disabled={hasActiveRun}
                  onSelect={() => {
                    if (!hasActiveRun) {
                      setMoveDialogOpen(true);
                    }
                  }}
                >
                  <FolderInput aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>
                    {hasActiveRun
                      ? "调整所属项目（运行中不可用）"
                      : "调整所属项目"}
                  </span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  destructive
                  onSelect={() => setDeleteDialogOpen(true)}
                >
                  <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                  <span>删除对话</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </div>
      </header>

      <MoveSessionProjectDialog
        onClose={() => setMoveDialogOpen(false)}
        session={moveDialogOpen ? session : null}
      />

      <DeleteSessionAlertDialog
        onClose={() => setDeleteDialogOpen(false)}
        session={deleteDialogOpen ? session : null}
      />
    </>
  );
});
