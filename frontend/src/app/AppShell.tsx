import { useEffect, useRef, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";

import { useAuth } from "../features/auth/AuthProvider";
import { authErrorMessage } from "../features/auth/errorMessages";
import { useLogoutMutation } from "../features/auth/useAuthMutations";
import { ChatSidebarContent } from "../features/chat/ChatSidebar";
import { DraftChatProvider } from "../features/chat/DraftChatContext";
import { KnowledgeSidebar } from "../features/knowledge/KnowledgeSidebar";
import { ProjectCenterSidebar } from "../features/projects/ProjectCenterSidebar";
import { PersonalSkillsSidebar } from "../features/skills/PersonalSkillsSidebar";
import { Brand } from "../shared/ui/Brand";
import { Icon, type IconName } from "../shared/ui/Icons";
import { Modal } from "../shared/ui/Modal";

interface NavigationItem {
  icon: IconName;
  label: string;
  to: string;
}

const BASE_NAVIGATION: NavigationItem[] = [
  { icon: "chat", label: "对话", to: "/chat" },
  { icon: "folder", label: "项目", to: "/projects" },
  { icon: "book", label: "知识库", to: "/knowledge" },
  { icon: "spark", label: "技能库", to: "/skills" },
];

const ADMIN_NAVIGATION: NavigationItem[] = [
  { icon: "sliders", label: "模型与压缩策略", to: "/admin/models" },
  { icon: "book", label: "系统提示词", to: "/admin/system-prompts" },
  { icon: "settings", label: "MCP Server", to: "/admin/mcp" },
  { icon: "tools", label: "工具审批策略", to: "/admin/tools" },
  { icon: "spark", label: "公共技能", to: "/admin/skills" },
  { icon: "chat", label: "子智能体", to: "/admin/subagents" },
];

function getDisplayName(displayName: string | null, email: string): string {
  return displayName?.trim() || email.split("@")[0] || "Athena 用户";
}

function getInitial(name: string): string {
  return Array.from(name.trim())[0]?.toUpperCase() ?? "A";
}

function WorkspaceSidebar() {
  const { pathname } = useLocation();
  const { user } = useAuth();
  if (!user) {
    return null;
  }

  const isAdminPath = pathname.startsWith("/admin");
  const isProjectPath = pathname.startsWith("/projects");
  const isKnowledgePath = pathname.startsWith("/knowledge");
  const isSkillsPath = pathname.startsWith("/skills");

  return (
    <aside className="workspace-sidebar" aria-label="工作区侧栏">
      <header className="sidebar-header">
        <Brand />
        <span className="sidebar-stage">工作区</span>
      </header>

      {isAdminPath ? (
        <div className="sidebar-scroll">
          <section className="sidebar-section" aria-labelledby="admin-nav-title">
            <div className="sidebar-section-heading">
              <span id="admin-nav-title">管理控制台</span>
            </div>
            {user.role === "admin" ? (
              <nav className="sidebar-links" aria-label="管理控制台">
                {ADMIN_NAVIGATION.map((item) => (
                  <NavLink
                    className={({ isActive }) =>
                      `sidebar-link${isActive ? " is-active" : ""}`
                    }
                    key={item.to}
                    to={item.to}
                  >
                    <Icon name={item.icon} size={16} />
                    <span>{item.label}</span>
                  </NavLink>
                ))}
              </nav>
            ) : (
              <div className="sidebar-guidance">
                <Icon name="shield" size={18} />
                <p>当前账号没有管理权限。</p>
              </div>
            )}
          </section>
        </div>
      ) : isProjectPath ? (
        <ProjectCenterSidebar />
      ) : isKnowledgePath ? (
        <KnowledgeSidebar />
      ) : isSkillsPath ? (
        <PersonalSkillsSidebar />
      ) : (
        <ChatSidebarContent />
      )}

    </aside>
  );
}

function AdminSubnav() {
  return (
    <nav className="admin-subnav" aria-label="管理子导航">
      <span className="admin-subnav-label">管理视图</span>
      {ADMIN_NAVIGATION.map((item) => (
        <NavLink
          className={({ isActive }) =>
            `admin-subnav-link${isActive ? " is-active" : ""}`
          }
          end
          key={item.to}
          to={item.to}
        >
          <Icon name={item.icon} size={15} />
          <span>{item.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

export function AppShell() {
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const logoutMutation = useLogoutMutation();
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const accountMenuRef = useRef<HTMLDivElement>(null);
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const profileLinkRef = useRef<HTMLAnchorElement>(null);
  const logoutButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // 路由切换后把焦点移到主内容，键盘和读屏用户不会停留在旧导航项。
    mainRef.current?.focus({ preventScroll: true });
  }, [location.pathname]);

  useEffect(() => {
    // 移动目录中的任意导航完成后自动关闭，避免遮罩继续阻断主内容。
    setDirectoryOpen(false);
  }, [location.key]);

  useEffect(() => {
    setAccountMenuOpen(false);
  }, [location.key]);

  useEffect(() => {
    if (accountMenuOpen) profileLinkRef.current?.focus();
  }, [accountMenuOpen]);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!accountMenuRef.current?.contains(event.target as Node)) {
        setAccountMenuOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [accountMenuOpen]);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setAccountMenuOpen(false);
        accountTriggerRef.current?.focus();
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [accountMenuOpen]);

  if (!user) {
    return null;
  }

  const name = getDisplayName(user.display_name, user.email);
  const initial = getInitial(name);
  const roleLabel = user.role === "admin" ? "管理员" : "普通用户";
  const isAdminSection =
    user.role === "admin" && location.pathname.startsWith("/admin");
  const navigation =
    user.role === "admin"
      ? [
          ...BASE_NAVIGATION,
          { icon: "settings" as const, label: "管理", to: "/admin" },
        ]
      : BASE_NAVIGATION;

  const handleLogout = async () => {
    setLogoutError(null);
    try {
      await logoutMutation.mutateAsync();
      navigate("/login", { replace: true });
    } catch (error) {
      setLogoutError(authErrorMessage(error));
    }
  };

  return (
    <DraftChatProvider>
      <div className="app-shell">
        <a className="skip-link" href="#main-content">
          跳到主要内容
        </a>

        <aside className="app-rail" aria-label="应用侧栏">
          <Link className="rail-brand" to="/chat" aria-label="Athena 对话首页">
            <Brand markOnly />
          </Link>

          <nav className="rail-nav" aria-label="主导航">
            {navigation.map((item) => (
              <NavLink
                aria-label={item.label}
                className={({ isActive }) =>
                  `rail-link${isActive ? " is-active" : ""}`
                }
                key={item.to}
                title={item.label}
                to={item.to}
              >
                <Icon name={item.icon} size={19} />
              </NavLink>
            ))}
          </nav>

          <div
            className="account-menu-anchor"
            onBlur={(event) => {
              if (
                !event.currentTarget.contains(event.relatedTarget as Node | null)
              ) {
                // 焦点用 Tab 离开账户控件后收起菜单，并保留正常的焦点目标。
                setAccountMenuOpen(false);
              }
            }}
            ref={accountMenuRef}
          >
            <button
              aria-expanded={accountMenuOpen}
              aria-haspopup="menu"
              aria-label="账户菜单"
              className="rail-avatar"
              onClick={() => setAccountMenuOpen((open) => !open)}
              onKeyDown={(event) => {
                if (event.key === "Escape" && accountMenuOpen) {
                  event.preventDefault();
                  setAccountMenuOpen(false);
                  accountTriggerRef.current?.focus();
                }
              }}
              ref={accountTriggerRef}
              title={`${name} · ${roleLabel}`}
              type="button"
            >
              <span aria-hidden="true">{initial}</span>
              <span className="sr-only">{name}，{roleLabel}</span>
            </button>
            {accountMenuOpen ? (
              <div
                className="account-menu"
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    setAccountMenuOpen(false);
                    accountTriggerRef.current?.focus();
                  } else if (
                    event.key === "ArrowDown" ||
                    event.key === "ArrowUp"
                  ) {
                    event.preventDefault();
                    const items = [
                      profileLinkRef.current,
                      logoutButtonRef.current,
                    ].filter(
                      (item): item is NonNullable<typeof item> => Boolean(item),
                    );
                    const currentIndex = items.findIndex(
                      (item) => item === document.activeElement,
                    );
                    const direction = event.key === "ArrowDown" ? 1 : -1;
                    const nextIndex =
                      (currentIndex + direction + items.length) % items.length;
                    items[nextIndex]?.focus();
                  }
                }}
                role="menu"
              >
                <div className="account-menu-identity">
                  <strong>{name}</strong>
                  <span title={user.email}>{user.email}</span>
                  <small>{roleLabel}</small>
                </div>
                <Link
                  className="account-menu-item"
                  onClick={() => setAccountMenuOpen(false)}
                  ref={profileLinkRef}
                  role="menuitem"
                  to="/profile"
                >
                  <Icon name="user" size={15} />
                  <span>个人页面</span>
                </Link>
                <button
                  className="account-menu-item account-menu-logout"
                  disabled={logoutMutation.isPending}
                  onClick={() => void handleLogout()}
                  ref={logoutButtonRef}
                  role="menuitem"
                  type="button"
                >
                  <Icon name="logout" size={15} />
                  <span>{logoutMutation.isPending ? "正在退出…" : "退出登录"}</span>
                </button>
              </div>
            ) : null}
          </div>
        </aside>

        {!directoryOpen ? (
          <WorkspaceSidebar />
        ) : null}

        <div className="main-column">
          <header className="mobile-topbar">
            <div className="mobile-topbar-actions">
              <button
                aria-controls="mobile-workspace-directory"
                aria-expanded={directoryOpen}
                aria-label="工作区目录"
                className="mobile-directory-button"
                onClick={() => setDirectoryOpen(true)}
                title="工作区目录"
                type="button"
              >
                <Icon name="folder" size={17} />
                <span>目录</span>
              </button>
            </div>
          </header>

          {logoutError && (
            <div className="shell-alert" role="alert" aria-live="polite">
              <Icon name="shield" size={16} />
              <span>{logoutError}</span>
            </div>
          )}

          {isAdminSection && <AdminSubnav />}

          <main
            className="workspace-main"
            id="main-content"
            ref={mainRef}
            tabIndex={-1}
          >
            <Outlet />
          </main>
        </div>
      </div>

      <Modal
        description="项目、未归属会话与最近记录"
        onClose={() => setDirectoryOpen(false)}
        open={directoryOpen}
        panelClassName="mobile-directory-dialog"
        title="工作区目录"
      >
        <div
          className="mobile-directory-shell"
          id="mobile-workspace-directory"
        >
          <WorkspaceSidebar />
        </div>
      </Modal>
    </DraftChatProvider>
  );
}
