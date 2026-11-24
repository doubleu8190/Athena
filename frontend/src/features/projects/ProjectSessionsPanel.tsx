import {
  ChevronLeft,
  ChevronRight,
  FolderInput,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  DeleteSessionAlertDialog,
  MoveSessionProjectDialog,
} from "../../app/shell/SessionActionDialogs";
import { formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  EmptyState,
  InlineAlert,
  Input,
  Skeleton,
  toast,
} from "../../shared/ui";
import { newChatHref } from "../chat/draft";
import { sessionErrorMessage } from "../sessions/errorMessages";
import {
  useSessionList,
  useUpdateSessionMutation,
} from "../sessions/hooks";
import {
  normalizeSessionSearchQuery,
  normalizeSessionTitle,
  sessionDisplayTitle,
  validateSessionTitle,
  type Session,
} from "../sessions/model";
import type { Project } from "./model";

const PAGE_SIZE = 10;
const SEARCH_DEBOUNCE_MS = 250;

function parsePage(value: string | null): number {
  if (!value) {
    return 1;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

interface RenameSessionRadixDialogProps {
  onClose: () => void;
  session: Session | null;
}

/** 项目内对话重命名弹窗（Radix Dialog）。 */
function RenameSessionRadixDialog({
  onClose,
  session,
}: RenameSessionRadixDialogProps) {
  const formId = useId();
  const errorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUpdateSessionMutation();
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (session) {
      setTitle(session.title ?? "");
      setValidationError(null);
      setRequestError(null);
      resetMutation();
    }
  }, [resetMutation, session]);

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && !mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    if (!session || mutation.isPending) {
      return;
    }
    const error = validateSessionTitle(title);
    if (error) {
      setValidationError(error);
      inputRef.current?.focus();
      return;
    }
    setValidationError(null);
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        input: { title: normalizeSessionTitle(title) },
        sessionId: session.id,
      });
      toast.success("对话标题已更新");
      onClose();
    } catch (failure) {
      setRequestError(sessionErrorMessage(failure));
    }
  };

  return (
    <Dialog onOpenChange={handleOpenChange} open={Boolean(session)}>
      <DialogContent
        closeDisabled={mutation.isPending}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          inputRef.current?.focus();
          inputRef.current?.select();
        }}
        size="sm"
      >
        <DialogHeader>
          <DialogTitle>重命名对话</DialogTitle>
          <DialogDescription>
            修改后将同步更新侧栏与项目对话列表中的显示标题。
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
                htmlFor={`${formId}-title`}
              >
                对话标题
              </label>
              <Input
                aria-describedby={validationError ? errorId : undefined}
                aria-label="对话标题"
                autoComplete="off"
                disabled={mutation.isPending}
                error={Boolean(validationError)}
                id={`${formId}-title`}
                onChange={(event) => {
                  setTitle(event.target.value);
                  if (validationError) setValidationError(null);
                  if (requestError) setRequestError(null);
                }}
                placeholder="输入新的对话标题"
                ref={inputRef}
                value={title}
              />
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
            保存标题
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface ProjectSessionsPanelProps {
  project: Project;
}

/**
 * 项目工作文件夹主列（左栏）：
 * 展示项目内全部历史对话流，支持标题防抖搜索（q 参数）、URL 分页同步与行内重命名/移动/删除。
 */
export function ProjectSessionsPanel({ project }: ProjectSessionsPanelProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = parsePage(searchParams.get("page"));
  const [searchInput, setSearchInput] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [renameSession, setRenameSession] = useState<Session | null>(null);
  const [moveSession, setMoveSession] = useState<Session | null>(null);
  const [deleteSession, setDeleteSession] = useState<Session | null>(null);

  useEffect(() => {
    const normalized = normalizeSessionSearchQuery(searchInput);
    if (!normalized) {
      setDebouncedQuery("");
      return;
    }
    const timer = window.setTimeout(() => {
      setDebouncedQuery(normalized);
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  // 搜索词变化时重置到第 1 页，避免停留在无结果的深分页。
  const previousQueryRef = useRef(debouncedQuery);
  useEffect(() => {
    if (previousQueryRef.current !== debouncedQuery) {
      previousQueryRef.current = debouncedQuery;
      if (page > 1) {
        setSearchParams({}, { replace: true });
      }
    }
  }, [debouncedQuery, page, setSearchParams]);

  const sessions = useSessionList({
    page,
    pageSize: PAGE_SIZE,
    projectId: project.id,
    ...(debouncedQuery ? { q: debouncedQuery } : {}),
  });

  const total = sessions.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  useEffect(() => {
    if (sessions.data && page > totalPages) {
      setSearchParams(
        totalPages > 1 ? { page: String(totalPages) } : {},
        { replace: true },
      );
    }
  }, [page, sessions.data, setSearchParams, totalPages]);

  const goToPage = (nextPage: number) => {
    setSearchParams(
      nextPage > 1 ? { page: String(nextPage) } : {},
      { replace: true },
    );
  };

  return (
    <section
      aria-labelledby="project-sessions-title"
      className="flex min-w-0 flex-col gap-4 rounded-xl border border-border-subtle bg-canvas p-5 shadow-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h2
            className="text-base font-semibold text-text-primary"
            id="project-sessions-title"
          >
            项目对话
          </h2>
          {sessions.isSuccess ? (
            <Badge variant="neutral">{total} 个</Badge>
          ) : null}
        </div>

        <div className="w-full sm:w-64" role="search">
          <Input
            aria-label="搜索项目内对话"
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="按标题搜索对话…"
            prefixSlot={<Search aria-hidden="true" className="h-4 w-4" />}
            suffixSlot={
              searchInput ? (
                <button
                  aria-label="清空搜索"
                  className="inline-flex h-5 w-5 items-center justify-center rounded text-text-tertiary hover:bg-muted hover:text-text-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40"
                  data-compact-control="true"
                  onClick={() => {
                    setSearchInput("");
                    setDebouncedQuery("");
                  }}
                  type="button"
                >
                  <X aria-hidden="true" className="h-3.5 w-3.5" />
                </button>
              ) : null
            }
            value={searchInput}
          />
        </div>
      </div>

      {sessions.isPending ? (
        <div className="space-y-2.5" role="status">
          <span className="sr-only">正在加载项目对话…</span>
          <Skeleton className="h-14 w-full rounded-lg" />
          <Skeleton className="h-14 w-full rounded-lg" />
          <Skeleton className="h-14 w-full rounded-lg" />
        </div>
      ) : sessions.isError ? (
        <InlineAlert
          action={
            <Button
              onClick={() => void sessions.refetch()}
              size="xs"
              type="button"
              variant="outline"
            >
              重试
            </Button>
          }
          description={sessionErrorMessage(sessions.error)}
          title="对话列表加载失败"
          variant="danger"
        />
      ) : sessions.data.items.length === 0 ? (
        debouncedQuery ? (
          <EmptyState
            action={
              <Button
                onClick={() => {
                  setSearchInput("");
                  setDebouncedQuery("");
                }}
                size="sm"
                type="button"
                variant="outline"
              >
                清空搜索条件
              </Button>
            }
            compact
            description={`没有找到标题包含「${debouncedQuery}」的项目对话。`}
            icon="search"
            title="未找到匹配的对话"
          />
        ) : (
          <EmptyState
            action={
              <Button asChild size="sm" variant="primary">
                <Link to={newChatHref(project.id)}>
                  <Plus aria-hidden="true" className="h-4 w-4" />
                  在此项目中开始对话
                </Link>
              </Button>
            }
            compact
            description="在此项目发起的新对话会自动应用右侧配置的专属指令、默认技能与关联知识库。"
            icon="chat"
            title="还没有项目对话"
          />
        )
      ) : (
        <>
          <ul
            aria-label="项目对话列表"
            className="divide-y divide-border-subtle rounded-lg border border-border-subtle"
          >
            {sessions.data.items.map((session) => {
              const title = sessionDisplayTitle(session.title);
              return (
                <li
                  className="group flex items-center justify-between gap-3 px-3.5 py-3 transition-colors hover:bg-subtle/70"
                  key={session.id}
                >
                  <Link
                    className="flex min-w-0 flex-1 items-center gap-3 rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40"
                    to={`/chat/${encodeURIComponent(session.id)}`}
                  >
                    <span
                      aria-hidden="true"
                      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-subtle text-brand"
                    >
                      <MessageSquare className="h-4 w-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-text-primary group-hover:text-brand">
                        {title}
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-text-tertiary">
                        <time dateTime={session.updated_at}>
                          更新于 {formatDateTime(session.updated_at)}
                        </time>
                      </div>
                    </div>
                  </Link>

                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        aria-label={`更多对话操作：${title}`}
                        data-icon-button="true"
                        size="icon"
                        type="button"
                        variant="ghost"
                      >
                        <MoreHorizontal aria-hidden="true" className="h-4 w-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => setRenameSession(session)}>
                        <Pencil aria-hidden="true" className="h-3.5 w-3.5" />
                        <span>重命名</span>
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => setMoveSession(session)}>
                        <FolderInput aria-hidden="true" className="h-3.5 w-3.5" />
                        <span>移动 / 移出项目</span>
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        destructive
                        onSelect={() => setDeleteSession(session)}
                      >
                        <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                        <span>删除对话</span>
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </li>
              );
            })}
          </ul>

          <footer className="flex flex-wrap items-center justify-between gap-2 pt-1 text-xs text-text-tertiary">
            <span>共 {total} 个对话</span>
            <nav aria-label="会话分页" className="flex items-center gap-1.5">
              <Button
                aria-label="上一页"
                disabled={page <= 1}
                onClick={() => goToPage(page - 1)}
                size="xs"
                type="button"
                variant="outline"
              >
                <ChevronLeft aria-hidden="true" className="h-3.5 w-3.5" />
              </Button>
              <span className="px-2 font-medium text-text-secondary">
                第 {page} / {totalPages} 页
              </span>
              <Button
                aria-label="下一页"
                disabled={page >= totalPages}
                onClick={() => goToPage(page + 1)}
                size="xs"
                type="button"
                variant="outline"
              >
                <ChevronRight aria-hidden="true" className="h-3.5 w-3.5" />
              </Button>
            </nav>
          </footer>
        </>
      )}

      <RenameSessionRadixDialog
        onClose={() => setRenameSession(null)}
        session={renameSession}
      />
      <MoveSessionProjectDialog
        onClose={() => setMoveSession(null)}
        session={moveSession}
      />
      <DeleteSessionAlertDialog
        onClose={() => setDeleteSession(null)}
        session={deleteSession}
      />
    </section>
  );
}
