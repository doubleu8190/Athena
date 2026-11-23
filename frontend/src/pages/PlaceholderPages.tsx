import { Link } from "react-router-dom";

import { Button, EmptyState } from "../shared/ui";

export function AccessDeniedPage() {
  return (
    <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center px-6 py-12">
      <EmptyState
        action={
          <Button asChild size="sm" variant="secondary">
            <Link to="/chat">返回对话工作台</Link>
          </Button>
        }
        description="当前页面仅对具备对应管理权限的企业管理员开放，你仍可正常使用工作台的其他功能。"
        icon="shield"
        title="当前账号没有管理员权限"
      />
    </div>
  );
}

/** 工作偏好与记忆过渡占位页（完整显式记忆与偏好设置将在 M2.6 交付）。 */
export function PreferencesPlaceholderPage() {
  return (
    <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center px-6 py-12">
      <EmptyState
        action={
          <div className="flex flex-wrap items-center justify-center gap-2">
            <Button asChild size="sm" variant="primary">
              <Link to="/profile">前往个人资料与安全</Link>
            </Button>
            <Button asChild size="sm" variant="outline">
              <Link to="/chat">返回对话工作台</Link>
            </Button>
          </div>
        }
        description="工作偏好与跨会话显式记忆管理正在建设中；当前可先在个人资料与安全页查看组织身份与修改密码。"
        icon="sliders"
        title="工作偏好与记忆"
      />
    </div>
  );
}
