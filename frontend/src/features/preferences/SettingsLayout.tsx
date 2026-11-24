import { ShieldCheck, SlidersHorizontal } from "lucide-react";
import type { PropsWithChildren } from "react";
import { NavLink } from "react-router-dom";

import { cn } from "../../shared/ui";

const SETTINGS_NAV_ITEMS = [
  {
    icon: SlidersHorizontal,
    label: "工作偏好与记忆",
    to: "/preferences",
  },
  {
    icon: ShieldCheck,
    label: "账号与安全",
    to: "/profile",
  },
] as const;

interface SettingsLayoutProps extends PropsWithChildren {
  /** 可选覆盖页头副标题，保持不同子页语义精准。 */
  description?: string;
}

/**
 * 个人设置共用壳层：为 /preferences（工作偏好与记忆）与 /profile（账号与安全）
 * 提供统一页头与基于 Router NavLink 的顶部导航切换条，保持键盘可达与当前路由高亮。
 */
export function SettingsLayout({
  children,
  description = "管理长期工作偏好、跨会话显式备忘，以及企业账号与登录安全设置。",
}: SettingsLayoutProps) {
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
      <header className="space-y-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold tracking-tight text-text-primary">
            个人设置
          </h1>
          <p className="text-sm text-text-secondary">{description}</p>
        </div>

        <nav
          aria-label="个人设置导航"
          className="flex items-center gap-6 border-b border-border-subtle"
        >
          {SETTINGS_NAV_ITEMS.map((item) => {
            const IconComponent = item.icon;
            return (
              <NavLink
                className={({ isActive }) =>
                  cn(
                    "-mb-px inline-flex items-center gap-2 border-b-2 px-1 py-2.5 text-sm font-medium whitespace-nowrap transition-colors",
                    "rounded-t-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring-focus/40",
                    isActive
                      ? "border-brand text-brand"
                      : "border-transparent text-text-secondary hover:border-border-strong hover:text-text-primary",
                  )
                }
                end
                key={item.to}
                to={item.to}
              >
                <IconComponent aria-hidden="true" className="h-4 w-4 shrink-0" />
                <span>{item.label}</span>
              </NavLink>
            );
          })}
        </nav>
      </header>

      <div className="space-y-6">{children}</div>
    </div>
  );
}
