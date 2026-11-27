import {
  BookOpen,
  Folder,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";

import { useAuth } from "../features/auth/AuthProvider";
import {
  DraftChatProvider,
  useDraftChat,
} from "../features/chat/DraftChatContext";
import { newChatHref } from "../features/chat/draft";
import { NewProjectDialog } from "../features/projects/ProjectNameDialogs";
import { useSession } from "../features/sessions/hooks";
import type { Session } from "../features/sessions/model";
import { useIsDesktopViewport } from "../shared/lib/viewport";
import {
  Brand,
  InlineAlert,
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  Tooltip,
  cn,
} from "../shared/ui";
import { AccountMenu } from "./shell/AccountMenu";
import {
  DeleteSessionAlertDialog,
  MoveSessionProjectDialog,
} from "./shell/SessionActionDialogs";
import { SidebarSessionList } from "./shell/SidebarSessionList";
import { SidebarWorkspaceNav } from "./shell/SidebarWorkspaceNav";
import {
  DESKTOP_BREAKPOINT_PX,
  activeSessionIdFromPath,
  getAdminNavigationSection,
  readSidebarCollapsed,
  writeSidebarCollapsed,
} from "./shell/navigation";

/** 判断事件目标是否位于可编辑输入控件内，防止全局快捷键误劫持文本输入。 */
function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  if (
    target.isContentEditable ||
    target.closest('[contenteditable="true"], [contenteditable=""]') !== null
  ) {
    return true;
  }
  const tagName = target.tagName;
  if (tagName === "INPUT" || tagName === "TEXTAREA" || tagName === "SELECT") {
    return true;
  }
  return target.getAttribute("role") === "textbox";
}

/** 检测 DOM 中是否存在除移动端导航抽屉外的模态弹窗（兼容 Radix Dialog / AlertDialog 与遗留 Modal）。 */
function hasBlockingModalDialog(): boolean {
  if (typeof document === "undefined") {
    return false;
  }
  const modalNodes = document.querySelectorAll(
    '[aria-modal="true"], [role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"]',
  );
  for (const node of modalNodes) {
    if (node.id !== "mobile-navigation-drawer") {
      return true;
    }
  }
  return false;
}

function AppShellLayout() {
  const { skillAdminAccessDenied, superAdminAccessDenied, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const { projectId: draftProjectId } = useDraftChat();

  const isDesktop = useIsDesktopViewport({
    breakpointPx: DESKTOP_BREAKPOINT_PX,
    strategy: "innerWidth",
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [moveSessionTarget, setMoveSessionTarget] = useState<Session | null>(
    null,
  );
  const [deleteSessionTarget, setDeleteSessionTarget] =
    useState<Session | null>(null);

  const mainRef = useRef<HTMLElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const pendingSearchFocusRef = useRef(false);
  const inlineEditingRef = useRef(false);
  const previousPathnameRef = useRef(location.pathname);

  const activeSessionId = activeSessionIdFromPath(location.pathname);
  const activeSession = useSession(activeSessionId);
  const isDraftRoute =
    location.pathname === "/chat" || location.pathname === "/chat/new";
  const contextProjectId =
    activeSession.data?.project_id ?? (isDraftRoute ? draftProjectId : null);
  const newChatTarget = newChatHref(contextProjectId);

  const adminSection = getAdminNavigationSection(user, {
    skillAdminAccessDenied,
    superAdminAccessDenied,
  });

  const handleEditingChange = useCallback((editing: boolean) => {
    inlineEditingRef.current = editing;
  }, []);

  const toggleSidebarCollapsed = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      writeSidebarCollapsed(next);
      return next;
    });
  }, []);

  const focusSidebarSearch = useCallback(() => {
    if (isDesktop) {
      if (sidebarCollapsed) {
        // 侧栏收起态下按 ⌘K 或点击搜索图标：先自动展开并持久化，再待输入框挂载后聚焦。
        pendingSearchFocusRef.current = true;
        setSidebarCollapsed(false);
        writeSidebarCollapsed(false);
      } else {
        searchInputRef.current?.focus();
        searchInputRef.current?.select();
      }
      return;
    }

    if (mobileDrawerOpen) {
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    } else {
      pendingSearchFocusRef.current = true;
      setMobileDrawerOpen(true);
    }
  }, [isDesktop, mobileDrawerOpen, sidebarCollapsed]);

  useEffect(() => {
    // 桌面端侧栏由收起态展开后，兑现待执行的搜索框聚焦意图。
    if (isDesktop && !sidebarCollapsed && pendingSearchFocusRef.current) {
      pendingSearchFocusRef.current = false;
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    }
  }, [isDesktop, sidebarCollapsed]);

  useEffect(() => {
    // 路由导航后自动关闭移动端抽屉，避免遮罩继续阻断主工作区。
    setMobileDrawerOpen(false);
  }, [location.key]);

  useEffect(() => {
    // 视口放大回到桌面端时收起移动端抽屉，保持单一侧栏形态。
    if (isDesktop) {
      setMobileDrawerOpen(false);
    }
  }, [isDesktop]);

  const hasActiveOverlayDialog =
    newProjectOpen ||
    moveSessionTarget !== null ||
    deleteSessionTarget !== null;

  useEffect(() => {
    // 路由路径变化时清理陈旧退出错误提示，并温和将焦点移至主工作区；
    // 若用户正在搜索、内联重命名或操作弹窗则不抢占焦点。
    if (previousPathnameRef.current === location.pathname) {
      return;
    }
    previousPathnameRef.current = location.pathname;
    setLogoutError(null);
    const isSearchFocused =
      searchInputRef.current !== null &&
      document.activeElement === searchInputRef.current;
    if (isSearchFocused || inlineEditingRef.current || hasActiveOverlayDialog) {
      return;
    }
    mainRef.current?.focus({ preventScroll: true });
  }, [hasActiveOverlayDialog, location.pathname]);

  useEffect(() => {
    // 全局工作台快捷键：⌘/Ctrl+B 折叠侧栏、⌘/Ctrl+K 聚焦搜索、⌘/Ctrl+Shift+O 新建对话。
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.altKey) {
        return;
      }
      const hasPrimaryModifier = event.metaKey || event.ctrlKey;
      if (!hasPrimaryModifier) {
        return;
      }

      // 任意业务弹窗打开或正在内联重命名时屏蔽全局壳层快捷键，避免穿透或打断当前事务。
      if (
        hasActiveOverlayDialog ||
        hasBlockingModalDialog() ||
        inlineEditingRef.current
      ) {
        return;
      }

      const inEditable =
        isEditableTarget(event.target) ||
        isEditableTarget(document.activeElement);
      const lowerKey = event.key.toLowerCase();

      if (!event.shiftKey && lowerKey === "b") {
        if (inEditable) {
          return;
        }
        event.preventDefault();
        if (isDesktop) {
          toggleSidebarCollapsed();
        } else {
          setMobileDrawerOpen((open) => !open);
        }
        return;
      }

      if (!event.shiftKey && lowerKey === "k") {
        // 在输入框中放行 macOS 原生 Ctrl+K（删除至行尾），同时保留 ⌘K 快速聚焦搜索。
        if (inEditable && event.ctrlKey && !event.metaKey) {
          return;
        }
        event.preventDefault();
        focusSidebarSearch();
        return;
      }

      if (event.shiftKey && lowerKey === "o") {
        if (inEditable) {
          return;
        }
        event.preventDefault();
        navigate(newChatTarget, { replace: false });
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    focusSidebarSearch,
    hasActiveOverlayDialog,
    isDesktop,
    navigate,
    newChatTarget,
    toggleSidebarCollapsed,
  ]);

  if (!user) {
    return null;
  }

  const isAdminPath = location.pathname.startsWith("/admin");

  const renderExpandedSidebarBody = (mode: "desktop" | "mobile") => (
    <>
      {mode === "desktop" ? (
        <header className="flex h-14 shrink-0 items-center justify-between gap-2 px-3">
          <Link
            aria-label="Athena 对话首页"
            className="min-w-0 rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            to="/chat"
          >
            <Brand compact />
          </Link>
          <Tooltip content="收起侧栏 ⌘B" side="right">
            <button
              aria-label="收起侧栏"
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-text-tertiary transition-colors hover:bg-muted hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={toggleSidebarCollapsed}
              type="button"
            >
              <PanelLeftClose aria-hidden="true" className="h-4 w-4" />
            </button>
          </Tooltip>
        </header>
      ) : (
        <div className="flex h-14 shrink-0 items-center border-b border-border-subtle px-4 pr-12">
          <SheetTitle className="text-sm font-semibold text-text-primary">
            工作区导航
          </SheetTitle>
          <SheetDescription className="sr-only">
            历史对话搜索、项目、工作台导航与账户设置
          </SheetDescription>
        </div>
      )}

      {/* 新建对话始终推送独立历史项，保留浏览器后退恢复旧草稿的能力。 */}
      <div className="shrink-0 px-3 pt-1 pb-2">
        <Link
          className="flex h-9 w-full items-center justify-between rounded-lg border border-border-default bg-canvas px-3 text-sm font-medium text-text-primary shadow-xs transition-colors hover:border-border-strong hover:bg-brand-subtle/40 hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
          replace={false}
          to={newChatTarget}
        >
          <span className="inline-flex items-center gap-2">
            <Plus aria-hidden="true" className="h-4 w-4 text-brand" />
            <span>新建对话</span>
          </span>
          <kbd
            aria-hidden="true"
            className="rounded border border-border-subtle bg-subtle px-1.5 py-0.5 font-mono text-[11px] font-normal text-text-tertiary"
          >
            ⌘⇧O
          </kbd>
        </Link>
      </div>

      <SidebarSessionList
        activeSessionId={activeSessionId}
        onEditingChange={handleEditingChange}
        onSelectDelete={setDeleteSessionTarget}
        onSelectMove={setMoveSessionTarget}
        searchInputRef={searchInputRef}
      />

      <SidebarWorkspaceNav onOpenNewProject={() => setNewProjectOpen(true)} />

      <AccountMenu onLogoutError={setLogoutError} user={user} />
    </>
  );

  const renderCollapsedSidebarBody = () => (
    <>
      <div className="flex shrink-0 flex-col items-center gap-2 pt-3 pb-2">
        <Tooltip content="Athena 对话首页" side="right">
          <Link
            aria-label="Athena 对话首页"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg transition-colors hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            to="/chat"
          >
            <Brand markOnly />
          </Link>
        </Tooltip>

        <Tooltip content="展开侧栏 ⌘B" side="right">
          <button
            aria-label="展开侧栏"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-text-secondary transition-colors hover:bg-muted hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            data-icon-button="true"
            onClick={toggleSidebarCollapsed}
            type="button"
          >
            <PanelLeftOpen aria-hidden="true" className="h-4 w-4" />
          </button>
        </Tooltip>
      </div>

      <div className="flex shrink-0 flex-col items-center gap-1 py-1">
        <Tooltip content="新建对话 ⌘⇧O" side="right">
          <Link
            aria-label="新建对话"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-border-default bg-canvas text-brand shadow-xs transition-colors hover:border-border-strong hover:bg-brand-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            data-icon-button="true"
            replace={false}
            to={newChatTarget}
          >
            <Plus aria-hidden="true" className="h-4 w-4" />
          </Link>
        </Tooltip>

        <Tooltip content="搜索历史对话 ⌘K" side="right">
          <button
            aria-label="搜索历史对话"
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-text-secondary transition-colors hover:bg-muted hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            data-icon-button="true"
            onClick={focusSidebarSearch}
            type="button"
          >
            <Search aria-hidden="true" className="h-4 w-4" />
          </button>
        </Tooltip>
      </div>

      <div aria-hidden="true" className="mx-auto my-1.5 h-px w-6 bg-border-subtle" />

      <nav
        aria-label="主导航"
        className="flex min-h-0 flex-1 flex-col items-center gap-1 overflow-y-auto py-1"
      >
        <Tooltip content="项目" side="right">
          <NavLink
            aria-label="项目"
            className={({ isActive }) =>
              cn(
                "inline-flex h-9 w-9 items-center justify-center rounded-lg transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                isActive
                  ? "bg-active text-brand"
                  : "text-text-secondary hover:bg-muted hover:text-text-primary",
              )
            }
            to="/projects"
          >
            <Folder aria-hidden="true" className="h-4 w-4" />
          </NavLink>
        </Tooltip>

        <Tooltip content="知识库" side="right">
          <NavLink
            aria-label="知识库"
            className={({ isActive }) =>
              cn(
                "inline-flex h-9 w-9 items-center justify-center rounded-lg transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                isActive
                  ? "bg-active text-brand"
                  : "text-text-secondary hover:bg-muted hover:text-text-primary",
              )
            }
            to="/knowledge"
          >
            <BookOpen aria-hidden="true" className="h-4 w-4" />
          </NavLink>
        </Tooltip>

        <Tooltip content="技能库" side="right">
          <NavLink
            aria-label="技能库"
            className={({ isActive }) =>
              cn(
                "inline-flex h-9 w-9 items-center justify-center rounded-lg transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                isActive
                  ? "bg-active text-brand"
                  : "text-text-secondary hover:bg-muted hover:text-text-primary",
              )
            }
            to="/skills"
          >
            <Sparkles aria-hidden="true" className="h-4 w-4" />
          </NavLink>
        </Tooltip>

        {adminSection ? (
          <Tooltip content={adminSection.collapsedLabel} side="right">
            <Link
              aria-current={isAdminPath ? "page" : undefined}
              aria-label={adminSection.collapsedLabel}
              className={cn(
                "inline-flex h-9 w-9 items-center justify-center rounded-lg transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                isAdminPath
                  ? "bg-active text-brand"
                  : "text-text-secondary hover:bg-muted hover:text-text-primary",
              )}
              to={adminSection.collapsedTarget}
            >
              <ShieldCheck aria-hidden="true" className="h-4 w-4" />
            </Link>
          </Tooltip>
        ) : null}
      </nav>

      <AccountMenu collapsed onLogoutError={setLogoutError} user={user} />
    </>
  );

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-subtle text-text-primary">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>

      {/* 桌面端（>=1024px）单层固定侧栏：支持展开态（240/260px）与 56px 精简图标栏平滑切换。 */}
      {isDesktop ? (
        <aside
          aria-label="工作区侧栏"
          className={cn(
            "flex h-full shrink-0 flex-col overflow-hidden border-r border-border-subtle bg-subtle transition-[width] duration-200 ease-out",
            sidebarCollapsed ? "w-[56px]" : "w-[240px] xl:w-[260px]",
          )}
          data-collapsed={sidebarCollapsed ? "true" : "false"}
        >
          {sidebarCollapsed
            ? renderCollapsedSidebarBody()
            : renderExpandedSidebarBody("desktop")}
        </aside>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-canvas">
        {/* <1024px 窄屏顶栏：左侧汉堡按钮与唯一 Brand 左对齐分组，右侧快捷新建对话。 */}
        {!isDesktop ? (
          <header className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-border-subtle bg-canvas px-3">
            <div className="flex min-w-0 items-center gap-2">
              <button
                aria-controls="mobile-navigation-drawer"
                aria-expanded={mobileDrawerOpen}
                aria-label="打开导航菜单"
                className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                data-icon-button="true"
                onClick={() => setMobileDrawerOpen(true)}
                type="button"
              >
                <Menu aria-hidden="true" className="h-5 w-5" />
              </button>

              <Link
                aria-label="Athena 对话首页"
                className="min-w-0 rounded-lg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                to="/chat"
              >
                <Brand compact />
              </Link>
            </div>

            <Link
              aria-label="新建对话"
              className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-brand transition-colors hover:bg-brand-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-icon-button="true"
              replace={false}
              to={newChatTarget}
            >
              <Plus aria-hidden="true" className="h-5 w-5" />
            </Link>
          </header>
        ) : null}

        {logoutError ? (
          <div className="shrink-0 px-4 pt-3">
            <InlineAlert description={logoutError} variant="danger" />
          </div>
        ) : null}

        <main
          className="min-h-0 min-w-0 flex-1 overflow-y-auto focus:outline-hidden"
          id="main-content"
          ref={mainRef}
          tabIndex={-1}
        >
          <Outlet />
        </main>
      </div>

      {!isDesktop ? (
        <Sheet onOpenChange={setMobileDrawerOpen} open={mobileDrawerOpen}>
          <SheetContent
            className="bg-subtle p-0"
            closeDisabled={hasActiveOverlayDialog}
            id="mobile-navigation-drawer"
            onEscapeKeyDown={(event) => {
              // 抽屉内正在内联重命名或搜索框非空时拦截 Escape，由内层控件先取消编辑或清空搜索，不直接关闭整个抽屉。
              const isSearchNonEmpty =
                searchInputRef.current !== null &&
                document.activeElement === searchInputRef.current &&
                searchInputRef.current.value.length > 0;
              if (inlineEditingRef.current || isSearchNonEmpty) {
                event.preventDefault();
              }
            }}
            onOpenAutoFocus={(event) => {
              if (pendingSearchFocusRef.current) {
                event.preventDefault();
                pendingSearchFocusRef.current = false;
                searchInputRef.current?.focus();
                searchInputRef.current?.select();
              }
            }}
            side="left"
          >
            {renderExpandedSidebarBody("mobile")}
          </SheetContent>
        </Sheet>
      ) : null}

      <NewProjectDialog
        onClose={() => setNewProjectOpen(false)}
        open={newProjectOpen}
      />

      <MoveSessionProjectDialog
        onClose={() => setMoveSessionTarget(null)}
        session={moveSessionTarget}
      />

      <DeleteSessionAlertDialog
        onClose={() => setDeleteSessionTarget(null)}
        session={deleteSessionTarget}
      />
    </div>
  );
}

export function AppShell() {
  return (
    <DraftChatProvider>
      <AppShellLayout />
    </DraftChatProvider>
  );
}
