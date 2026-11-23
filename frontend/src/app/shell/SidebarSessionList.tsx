import {
  FolderInput,
  Loader2,
  MoreHorizontal,
  Pencil,
  RotateCw,
  Search,
  Trash2,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { Link } from "react-router-dom";

import { sessionErrorMessage } from "../../features/sessions/errorMessages";
import {
  useSessionList,
  useUpdateSessionMutation,
} from "../../features/sessions/hooks";
import {
  SESSION_TITLE_MAX_LENGTH,
  groupSessionsByTime,
  normalizeSessionSearchQuery,
  normalizeSessionTitle,
  sessionDisplayTitle,
  validateSessionTitle,
  type Session,
} from "../../features/sessions/model";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Input,
  Skeleton,
  cn,
  toast,
} from "../../shared/ui";

const SESSION_SEARCH_DEBOUNCE_MS = 250;
const SIDEBAR_SESSION_PAGE_SIZE = 30;

interface SidebarSessionRowProps {
  isActive: boolean;
  isEditing: boolean;
  onFinishRename: (sessionId: string) => void;
  onSelectDelete: (session: Session) => void;
  onSelectMove: (session: Session) => void;
  onStartRename: (sessionId: string) => void;
  session: Session;
}

/** 单条侧栏会话行：支持选中高亮、原地内联重命名状态机及更多操作 DropdownMenu。 */
function SidebarSessionRow({
  isActive,
  isEditing,
  onFinishRename,
  onSelectDelete,
  onSelectMove,
  onStartRename,
  session,
}: SidebarSessionRowProps) {
  const updateSessionMutation = useUpdateSessionMutation();
  const [draftTitle, setDraftTitle] = useState(session.title ?? "");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [focusRetryToken, setFocusRetryToken] = useState(0);

  const inputRef = useRef<HTMLInputElement>(null);
  // 防止按下 Esc 取消或 Enter 提交后紧随的 blur 事件重复触发保存。
  const cancelGuardRef = useRef(false);
  const submittingRef = useRef(false);
  // 记录上次服务端失败时的草稿值，避免失焦时对同一失败输入反复重发 PATCH。
  const lastFailedDraftRef = useRef<string | null>(null);
  // 从 DropdownMenu 切入内联重命名时，阻止 Radix 关闭浮层后将焦点抢回已卸载的触发按钮。
  const pendingRenameFocusRef = useRef(false);
  // 追踪最新服务端标题与上一次编辑态，确保仅在 false -> true 进入编辑时初始化草稿，
  // 避免后台列表刷新覆盖用户正在输入的文本。
  const latestTitleRef = useRef(session.title);
  latestTitleRef.current = session.title;
  const wasEditingRef = useRef(false);

  useEffect(() => {
    const enteredEditing = isEditing && !wasEditingRef.current;
    wasEditingRef.current = isEditing;
    if (!enteredEditing) {
      return;
    }
    setDraftTitle(latestTitleRef.current ?? "");
    setInlineError(null);
    cancelGuardRef.current = false;
    submittingRef.current = false;
    lastFailedDraftRef.current = null;
    // 在下一帧聚焦并全选，避免与菜单关闭周期竞争焦点。
    const frameId = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [isEditing, session.id]);

  useEffect(() => {
    // 重命名失败后等待 mutation.isPending 解除（输入框不再 disabled）再恢复焦点。
    if (!isEditing || focusRetryToken === 0 || updateSessionMutation.isPending) {
      return;
    }
    const frameId = window.requestAnimationFrame(() => {
      inputRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frameId);
  }, [focusRetryToken, isEditing, updateSessionMutation.isPending]);

  const cancelRename = () => {
    cancelGuardRef.current = true;
    submittingRef.current = false;
    lastFailedDraftRef.current = null;
    setInlineError(null);
    setDraftTitle(latestTitleRef.current ?? "");
    onFinishRename(session.id);
  };

  const commitRename = async (trigger: "blur" | "enter") => {
    if (cancelGuardRef.current || submittingRef.current) {
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
    // 标题实质未变时直接退出编辑态，不发送多余 PATCH 请求。
    if (normalizedCurrent && normalizedNext === normalizedCurrent) {
      setInlineError(null);
      onFinishRename(session.id);
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
      // 传入当前 session.id，由父层按行比对关闭，防止迟到成功回调串号关掉其他行的编辑框。
      onFinishRename(session.id);
    } catch (error) {
      submittingRef.current = false;
      lastFailedDraftRef.current = draftTitle;
      setInlineError(sessionErrorMessage(error));
      setFocusRetryToken((token) => token + 1);
    }
  };

  const displayTitle = sessionDisplayTitle(session.title);

  if (isEditing) {
    return (
      <li className="px-0.5 py-0.5">
        <div className="rounded-lg border border-ring-focus bg-canvas p-1 shadow-xs ring-2 ring-ring-focus/15">
          <div className="flex items-center gap-1">
            <input
              aria-invalid={Boolean(inlineError) || undefined}
              aria-label="重命名会话标题"
              autoComplete="off"
              className="h-7 min-w-0 flex-1 border-0 bg-transparent px-1.5 text-[13px] text-text-primary placeholder:text-text-placeholder focus-visible:outline-hidden"
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
                className="mr-1 h-3.5 w-3.5 shrink-0 animate-spin text-text-tertiary motion-reduce:animate-none"
              />
            ) : (
              <button
                aria-label="取消重命名"
                className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-text-tertiary transition-colors hover:bg-muted hover:text-text-primary"
                data-compact-control="true"
                onClick={cancelRename}
                onMouseDown={(event) => {
                  // 阻止点击取消按钮时先触发输入框 blur 保存。
                  event.preventDefault();
                }}
                type="button"
              >
                <X aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>
        {inlineError ? (
          <p className="mt-1 px-1.5 text-xs leading-snug text-danger" role="alert">
            {inlineError}
          </p>
        ) : null}
      </li>
    );
  }

  return (
    <li
      className={cn(
        "group relative flex items-center rounded-lg transition-colors",
        isActive
          ? "bg-active font-medium text-brand"
          : "text-text-secondary hover:bg-muted/80 hover:text-text-primary",
      )}
    >
      {isActive ? (
        <span
          aria-hidden="true"
          className="absolute top-1.5 bottom-1.5 left-0 w-[3px] rounded-full bg-brand"
        />
      ) : null}

      <Link
        aria-current={isActive ? "page" : undefined}
        className="min-w-0 flex-1 truncate rounded-lg py-1.5 pr-7 pl-2.5 text-[13px] leading-5 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus"
        title={displayTitle}
        to={`/chat/${encodeURIComponent(session.id)}`}
      >
        {displayTitle}
      </Link>

      <DropdownMenu modal={false} onOpenChange={setMenuOpen} open={menuOpen}>
        <DropdownMenuTrigger asChild>
          <button
            aria-label="更多会话操作"
            className={cn(
              "absolute right-1 inline-flex h-6 w-6 items-center justify-center rounded-md text-text-tertiary transition-opacity",
              "hover:bg-canvas hover:text-text-primary",
              "focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus",
              menuOpen
                ? "opacity-100"
                : "opacity-0 group-focus-within:opacity-100 group-hover:opacity-100",
            )}
            data-compact-control="true"
            type="button"
          >
            <MoreHorizontal aria-hidden="true" className="h-3.5 w-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          onCloseAutoFocus={(event) => {
            if (pendingRenameFocusRef.current) {
              event.preventDefault();
              pendingRenameFocusRef.current = false;
            }
          }}
          side="bottom"
        >
          <DropdownMenuItem
            onSelect={() => {
              pendingRenameFocusRef.current = true;
              onStartRename(session.id);
            }}
          >
            <Pencil aria-hidden="true" className="h-3.5 w-3.5 text-text-tertiary" />
            <span>重命名</span>
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onSelectMove(session)}>
            <FolderInput
              aria-hidden="true"
              className="h-3.5 w-3.5 text-text-tertiary"
            />
            <span>{session.project_id ? "移动 / 移出项目" : "移动至项目"}</span>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem destructive onSelect={() => onSelectDelete(session)}>
            <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
            <span>删除对话</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

interface SidebarSessionListProps {
  activeSessionId: string | null;
  onEditingChange: (editing: boolean) => void;
  onSelectDelete: (session: Session) => void;
  onSelectMove: (session: Session) => void;
  searchInputRef: RefObject<HTMLInputElement>;
}

/** 侧栏实时搜索框 + 会话历史独立滚动列表（默认按时间分组，搜索时平铺展示）。 */
export function SidebarSessionList({
  activeSessionId,
  onEditingChange,
  onSelectDelete,
  onSelectMove,
  searchInputRef,
}: SidebarSessionListProps) {
  const [searchText, setSearchText] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);

  const trimmedSearch = normalizeSessionSearchQuery(searchText);
  const isSearchActive = debouncedQuery.length > 0;

  useEffect(() => {
    // 清空关键词立即切回分组历史，避免无意义延迟；输入关键词时做 250ms 防抖。
    if (!trimmedSearch) {
      setDebouncedQuery("");
      return;
    }
    const timer = window.setTimeout(() => {
      setDebouncedQuery(trimmedSearch);
    }, SESSION_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [trimmedSearch]);

  useEffect(() => {
    onEditingChange(editingSessionId !== null);
    // 组件卸载（如侧栏收起或移动端抽屉关闭）时重置父层编辑标记，防止快捷键与焦点逻辑长期卡死。
    return () => {
      onEditingChange(false);
    };
  }, [editingSessionId, onEditingChange]);

  useEffect(() => {
    // 切换搜索关键词或进入/退出搜索态时收敛旧编辑态，避免跨视图泄漏。
    setEditingSessionId(null);
  }, [debouncedQuery]);

  const finishRename = useCallback((sessionId: string) => {
    setEditingSessionId((current) =>
      current === sessionId ? null : current,
    );
  }, []);

  const recentQuery = useSessionList(
    { page: 1, pageSize: SIDEBAR_SESSION_PAGE_SIZE },
    !isSearchActive,
  );
  const searchQuery = useSessionList(
    { page: 1, pageSize: SIDEBAR_SESSION_PAGE_SIZE, q: debouncedQuery },
    isSearchActive,
  );

  const groupedRecent = useMemo(
    () => groupSessionsByTime(recentQuery.data?.items ?? []),
    [recentQuery.data?.items],
  );

  const clearSearch = () => {
    setSearchText("");
    setDebouncedQuery("");
    setEditingSessionId(null);
    searchInputRef.current?.focus();
  };

  const activeQuery = isSearchActive ? searchQuery : recentQuery;
  const searchItems = searchQuery.data?.items ?? [];
  const searchTotal = searchQuery.data?.total ?? searchItems.length;

  return (
    <div className="flex min-h-[140px] flex-1 flex-col overflow-hidden">
      <div
        aria-label="搜索历史对话"
        className="shrink-0 px-3 pt-1 pb-2"
        role="search"
      >
        <Input
          aria-label="搜索历史对话"
          className="h-8 rounded-lg bg-canvas px-2.5 text-xs shadow-none"
          inputClassName="text-xs"
          maxLength={SESSION_TITLE_MAX_LENGTH}
          onChange={(event) => setSearchText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape" && searchText) {
              event.preventDefault();
              event.stopPropagation();
              clearSearch();
            }
          }}
          placeholder="搜索历史对话…"
          prefixSlot={<Search aria-hidden="true" className="h-3.5 w-3.5" />}
          ref={searchInputRef}
          suffixSlot={
            searchText ? (
              <button
                aria-label="清空搜索"
                className="inline-flex h-5 w-5 items-center justify-center rounded text-text-tertiary transition-colors hover:bg-muted hover:text-text-primary"
                data-compact-control="true"
                onClick={clearSearch}
                type="button"
              >
                <X aria-hidden="true" className="h-3.5 w-3.5" />
              </button>
            ) : null
          }
          value={searchText}
        />
      </div>

      <div
        aria-label="历史对话列表"
        className="min-h-0 flex-1 overflow-y-auto px-2 pb-2"
      >
        {activeQuery.isPending ? (
          <div aria-label="正在加载会话" className="space-y-1.5 px-1 py-1" role="status">
            <Skeleton className="h-7 w-full rounded-lg" />
            <Skeleton className="h-7 w-5/6 rounded-lg" />
            <Skeleton className="h-7 w-4/5 rounded-lg" />
            <Skeleton className="h-7 w-11/12 rounded-lg" />
          </div>
        ) : activeQuery.isError ? (
          <div
            className="mx-1 rounded-lg border border-border-subtle bg-canvas px-2.5 py-2 text-xs text-text-secondary"
            role="status"
          >
            <p className="text-danger">
              {isSearchActive ? "搜索对话失败" : "会话历史加载失败"}
            </p>
            <button
              className="mt-1.5 inline-flex items-center gap-1 font-medium text-brand hover:underline"
              data-compact-control="true"
              onClick={() => void activeQuery.refetch()}
              type="button"
            >
              <RotateCw aria-hidden="true" className="h-3 w-3" />
              重新加载
            </button>
          </div>
        ) : isSearchActive ? (
          <section aria-label="搜索结果" className="space-y-1">
            <div className="flex items-center justify-between px-2 py-1 text-[11px] font-medium text-text-tertiary">
              <span>搜索结果（{searchTotal}）</span>
              <button
                className="text-brand hover:underline"
                data-compact-control="true"
                onClick={clearSearch}
                type="button"
              >
                清空
              </button>
            </div>
            {searchItems.length === 0 ? (
              <div className="px-2 py-4 text-center text-xs text-text-tertiary">
                <p>未找到匹配的对话</p>
                <p className="mt-1 text-[11px]">试试更简短的关键词</p>
              </div>
            ) : (
              <ul className="space-y-0.5">
                {searchItems.map((session) => (
                  <SidebarSessionRow
                    isActive={activeSessionId === session.id}
                    isEditing={editingSessionId === session.id}
                    key={session.id}
                    onFinishRename={finishRename}
                    onSelectDelete={onSelectDelete}
                    onSelectMove={onSelectMove}
                    onStartRename={setEditingSessionId}
                    session={session}
                  />
                ))}
              </ul>
            )}
          </section>
        ) : groupedRecent.length === 0 ? (
          <div className="px-2.5 py-4 text-xs text-text-tertiary">
            还没有对话记录
          </div>
        ) : (
          <div className="space-y-3">
            {groupedRecent.map((group) => (
              <section aria-label={group.label} key={group.key}>
                <h2 className="px-2.5 pb-1 text-[11px] font-medium text-text-tertiary">
                  {group.label}
                </h2>
                <ul className="space-y-0.5">
                  {group.items.map((session) => (
                    <SidebarSessionRow
                      isActive={activeSessionId === session.id}
                      isEditing={editingSessionId === session.id}
                      key={session.id}
                      onFinishRename={finishRename}
                      onSelectDelete={onSelectDelete}
                      onSelectMove={onSelectMove}
                      onStartRename={setEditingSessionId}
                      session={session}
                    />
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
