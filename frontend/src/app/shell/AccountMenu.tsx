import {
  ChevronsUpDown,
  LogOut,
  SlidersHorizontal,
  UserRound,
} from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

import { authErrorMessage } from "../../features/auth/errorMessages";
import {
  getPrimaryDepartment,
  getUserDisplayName,
  getUserRoleLabel,
  type User,
} from "../../features/auth/model";
import { useLogoutMutation } from "../../features/auth/useAuthMutations";
import {
  Badge,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Tooltip,
  cn,
  toast,
} from "../../shared/ui";
import { getCompactRoleBadge, getUserInitial } from "./navigation";

interface AccountMenuProps {
  collapsed?: boolean;
  onLogoutError: (message: string | null) => void;
  user: User;
}

/** 侧栏底部固定账户区与向上弹出的 Radix DropdownMenu，展开态与 56px 收起态共用同一套菜单项。 */
export function AccountMenu({
  collapsed = false,
  onLogoutError,
  user,
}: AccountMenuProps) {
  const navigate = useNavigate();
  const logoutMutation = useLogoutMutation();

  const displayName = getUserDisplayName(user);
  const initial = getUserInitial(displayName);
  const primaryDepartment = getPrimaryDepartment(user);
  const roleLabel = getUserRoleLabel(user.role);
  const compactBadge = getCompactRoleBadge(user.role);

  const secondaryLine =
    primaryDepartment?.name ??
    user.email ??
    (user.employee_no ? `工号 ${user.employee_no}` : null);

  const contactLine =
    user.email ?? (user.employee_no ? `工号 ${user.employee_no}` : null);

  const handleLogout = async () => {
    onLogoutError(null);
    try {
      await logoutMutation.mutateAsync();
      navigate("/login", { replace: true });
    } catch (error) {
      const message = authErrorMessage(error);
      onLogoutError(message);
      toast.error(message);
    }
  };

  return (
    <div
      className={cn(
        "shrink-0 border-t border-border-subtle",
        collapsed ? "flex justify-center py-2.5" : "p-2",
      )}
    >
      {/* 使用非模态 DropdownMenu：点击页面任意外部控件可一次性收起并继续交互，避免锁定 body pointer-events。 */}
      <DropdownMenu modal={false}>
        <Tooltip
          content={`${displayName} · ${roleLabel}`}
          disabled={!collapsed}
          side="right"
        >
          <DropdownMenuTrigger asChild>
            {collapsed ? (
              <button
                aria-label="账户菜单"
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-brand-border bg-brand-subtle text-xs font-semibold text-brand transition-colors hover:bg-active focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                data-icon-button="true"
                type="button"
              >
                <span aria-hidden="true">{initial}</span>
              </button>
            ) : (
              <button
                aria-label="账户菜单"
                className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors hover:bg-muted/80 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus"
                type="button"
              >
                <span
                  aria-hidden="true"
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-brand-border bg-brand-subtle text-xs font-semibold text-brand"
                >
                  {initial}
                </span>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium text-text-primary">
                      {displayName}
                    </span>
                    <Badge
                      className="shrink-0 px-1.5 py-0 text-[10px] leading-4"
                      variant={compactBadge.variant}
                    >
                      {compactBadge.label}
                    </Badge>
                  </span>
                  {secondaryLine ? (
                    <span className="truncate text-[11px] text-text-tertiary">
                      {secondaryLine}
                    </span>
                  ) : null}
                </span>
                <ChevronsUpDown
                  aria-hidden="true"
                  className="h-3.5 w-3.5 shrink-0 text-text-tertiary"
                />
              </button>
            )}
          </DropdownMenuTrigger>
        </Tooltip>

        <DropdownMenuContent
          align={collapsed ? "end" : "start"}
          className="w-60"
          side={collapsed ? "right" : "top"}
          sideOffset={8}
        >
          <div className="px-2.5 py-2">
            <div className="truncate text-sm font-semibold text-text-primary">
              {displayName}
            </div>
            {contactLine ? (
              <div
                className="mt-0.5 truncate text-xs text-text-tertiary"
                title={contactLine}
              >
                {contactLine}
              </div>
            ) : null}
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              {primaryDepartment ? (
                <Badge variant="neutral">{primaryDepartment.name}</Badge>
              ) : null}
              <Badge variant={compactBadge.variant}>{roleLabel}</Badge>
            </div>
          </div>

          <DropdownMenuSeparator />

          <DropdownMenuItem asChild>
            <Link to="/preferences">
              <SlidersHorizontal
                aria-hidden="true"
                className="h-4 w-4 text-text-tertiary"
              />
              <span>工作偏好与记忆</span>
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/profile">
              <UserRound
                aria-hidden="true"
                className="h-4 w-4 text-text-tertiary"
              />
              <span>个人资料与安全</span>
            </Link>
          </DropdownMenuItem>

          <DropdownMenuSeparator />

          <DropdownMenuItem
            destructive
            disabled={logoutMutation.isPending}
            onSelect={() => {
              void handleLogout();
            }}
          >
            <LogOut aria-hidden="true" className="h-4 w-4" />
            <span>{logoutMutation.isPending ? "正在退出…" : "退出登录"}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
